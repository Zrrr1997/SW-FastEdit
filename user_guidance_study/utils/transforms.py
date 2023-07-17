# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import random
import warnings
import time
from typing import Dict, Hashable, List, Mapping, Optional, Union, Iterable
import gc
import math

from enum import IntEnum
from pynvml import *

import numpy as np
np.seterr(all='raise')
import torch
import pandas as pd
from numpy.typing import ArrayLike

from monai.config import KeysCollection
from monai.data import MetaTensor
from monai.networks.layers import GaussianFilter
from monai.transforms.transform import MapTransform, Randomizable, Transform
from monai.transforms import CenterSpatialCropd, Compose, CropForegroundd
from monai.utils import min_version, optional_import
from monai.data.meta_tensor import MetaTensor
from monai.metrics import compute_dice
from monai.data import PatchIterd
from monai.losses import DiceLoss
from monai.transforms import (
    Activationsd,
    AsDiscreted,
)
from monai.metrics.meandice import DiceMetric


import cupy as cp
# Details here: https://docs.rapids.ai/api/cucim/nightly/api/#cucim.core.operations.morphology.distance_transform_edt
from cucim.core.operations.morphology import distance_transform_edt as distance_transform_edt_cupy
from cupyx.scipy.ndimage import label as label_cp

from utils.distance_transform import get_distance_transform, get_choice_from_distance_transform_cp, get_choice_from_tensor

from utils.helper import (print_gpu_usage, print_tensor_gpu_usage, describe, describe_batch_data, timeit, get_tensor_at_coordinates, get_global_coordinates_from_patch_coordinates)
from utils.logger import setup_loggers, get_logger

from monai.utils.enums import CommonKeys

logger = None

class ClickGenerationStrategy(IntEnum):
    # Sample a click randomly based on the label, so no correction based on the prediction
    GLOBAL_NON_CORRECTIVE = 1
    # Sample a click based on the discrepancy between label and predition
    # Thus generate corrective clicks where the networks predicts incorrectly so far
    GLOBAL_CORRECTIVE = 2
    # Subdivide volume into patches of size train_crop_size, calculate the dice score for each, then sample click on the worst one
    PATCH_BASED_CORRECTIVE = 3
    # At each iteration sample from the probability and don't add a click if it yields False
    DEEPGROW_GLOBAL_CORRECTIVE = 4



class StoppingCriterion(IntEnum):
    # Sample max_train_interactions amount of clicks (can be done in the first iteration if non-corrective)
    MAX_ITER = 1
    # Sample clicks iteratively. At each step sample p~(0,1). If p > x continue sampling
    MAX_ITER_AND_PROBABILITY = 2
    # Sample clicks iteratively. Stop when dice good enough (e.g. 0.9) or when max_train_interactions amount of clicks
    MAX_ITER_AND_DICE = 3
    # Sample clicks iteratively. At each step: Stop if max_train_interactions is reached. Otherwise sample p~(0,1).
# If p > dice continue sampling, then check if dice is good enough. If so no more clicks are required.
    MAX_ITER_PROBABILITY_AND_DICE = 4
     # Stopping as previously implemented with Deepgrow
    DEEPGROW_PROBABILITY = 5

def threshold_foreground(x):
    return x > 0.005

class NoOpd(MapTransform):
    def __init__(self, keys: KeysCollection = None):
        """
        A transform which does nothing
        """
        super().__init__(keys)

    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        return data


class CheckTheAmountOfInformationLossByCropd(MapTransform):
    def __init__(self, keys: KeysCollection, roi_size:Iterable, label_names:Dict):
        """
        Prints how much information is lost due to the crop.
        """
        super().__init__(keys)
        self.roi_size = roi_size
        self.label_names = label_names

    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        d = data
        for key in self.key_iterator(d):
            if key == "label":
                label = d[key]
                new_data = {"label": label.clone(), "image": d["image"].clone()}
                # copy the label and crop it to the desired size
                t = []
                t.append(CropForegroundd(keys=("image", "label"), source_key="image", select_fn=threshold_foreground))
                if self.roi_size is not None:
                    t.append(CenterSpatialCropd(keys="label", roi_size=self.roi_size))
                
                cropped_label = Compose(t)(new_data)["label"]

                # label_num_el = torch.numel(label)
                for idx, (key_label, val_label) in enumerate(self.label_names.items(), start=1):
                    # Only count non-background lost labels
                    if key_label != "background":
                        sum_label = torch.sum(label == idx).item()
                        sum_cropped_label = torch.sum(cropped_label == idx).item()
                        # then check how much of the labels is lost
                        lost_pixels = sum_label - sum_cropped_label
                        lost_pixels_ratio = lost_pixels / sum_label * 100
                        logger.info(f"{lost_pixels_ratio:.1f} % of labelled pixels of the type {key_label} have been lost when cropping") 
            else: 
                raise UserWarning("This transform only applies to key 'label'")
        return d

class PrintDatad(MapTransform):
    def __init__(self, keys: KeysCollection = None):
        """
        Prints all the information inside data
        """
        super().__init__(keys)

    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        d = data
        logger.info(describe_batch_data(d))
        # exit(0)
        return d

class PrintGPUUsaged(MapTransform):
    def __init__(self, device, keys: KeysCollection = None):
        """
        Prints the GPU usage
        """
        super().__init__(keys)
        self.device = device

    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        d = data
        logger.info(f"Current reserved memory for dataloader: {torch.cuda.memory_reserved(self.device) / (1024**3)} GB")
        return d

class ClearGPUMemoryd(MapTransform):
    def __init__(self, device, keys: KeysCollection = None, garbage_collection: bool = True):
        """
        Prints the GPU usage
        """
        super().__init__(keys)
        self.device = device
        self.garbage_collection = garbage_collection

    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        d = data
        if self.garbage_collection:
            gc.collect()
        torch.cuda.empty_cache()
        if logger is not None:
            logger.info(f"Current reserved memory for dataloader: {torch.cuda.memory_reserved(self.device) / (1024**3)} GB")
        return d


class InitLoggerd(MapTransform):
    def __init__(self, args):
        """ 
        Initialises the logger inside the dataloader thread (if it is a separate thread).

        Has to be reinitialized for some weird reason here, I think this is due to the data transform
        being on an extra thread
        Otherwise the logger only works for the click_transforms and never for the pre_transform
        """
        global logger
        super().__init__(None)
        
        self.loglevel = logging.INFO
        if args.debug:
            self.loglevel = logging.DEBUG

        self.log_file_folder = args.output
        if args.no_log: 
            self.log_file_folder = None
        setup_loggers(self.loglevel, self.log_file_folder)
        logger = get_logger()


    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        global logger
        if logger is None: 
            setup_loggers(self.loglevel, self.log_file_folder)
        logger = get_logger()
        return data


class NormalizeLabelsInDatasetd(MapTransform):
    def __init__(self, keys: KeysCollection, label_names=None, allow_missing_keys: bool = False, device = None):
        """
        Normalize label values according to label names dictionary

        Args:
            keys: The ``keys`` parameter will be used to get and set the actual data item to transform
            label_names: all label names
        """
        super().__init__(keys, allow_missing_keys)
        self.label_names = label_names
        self.device = device
    
    @timeit
    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        # d: Dict = dict(data)
        d = data

        # if len(self.label_names.items()) <= 2:
        #     d["label_names"] = self.label_names
        # else:
        for key in self.key_iterator(d):
            # Dictionary containing new label numbers
            new_label_names = {}
            label = torch.zeros(d[key].shape, device=self.device)
            # Making sure the range values and number of labels are the same
            for idx, (key_label, val_label) in enumerate(self.label_names.items(), start=1):
                if key_label == "background":
                    new_label_names["background"] = 0
                else:
                    new_label_names[key_label] = idx
                    label[d[key] == val_label] = idx

            d["label_names"] = new_label_names
            if isinstance(d[key], MetaTensor):
                d[key].array = label #.to(torch.device("cpu"))
            else:
                d[key] = label #.to(torch.device("cpu"))
        return d



class AddGuidanceSignalDeepEditd(MapTransform):
    """
    Add Guidance signal for input image. Multilabel DeepEdit

    Based on the "guidance" points, apply Gaussian to them and add them as new channel for input image.

    Args:
        guidance_key: key to store guidance.
        sigma: standard deviation for Gaussian kernel.
        number_intensity_ch: channel index.
    """

    def __init__(
        self,
        keys: KeysCollection,
        guidance_key: str = "guidance",
        sigma: int = 3,
        number_intensity_ch: int = 1,
        allow_missing_keys: bool = False,
        disks: bool = False,
        edt: bool = False,
        gdt: bool = False,
        gdt_th: float = 0.1,
        exp_geos: bool = False,
        device = None,
        spacing = None,
        adaptive_sigma = False,
        click_generation = 2,
        val_click_generation = 2,
    ):
        super().__init__(keys, allow_missing_keys)
        self.guidance_key = guidance_key
        self.sigma = sigma
        self.number_intensity_ch = number_intensity_ch
        self.disks = disks
        self.edt = edt
        self.gdt = gdt
        self.gdt_th = gdt_th
        self.exp_geos = exp_geos
        self.device = device
        self.spacing = spacing
        self.adaptive_sigma = adaptive_sigma
        self.gdt_th = 0 if self.exp_geos else self.gdt_th
        self.gdt = True if self.exp_geos else self.gdt


    def _get_corrective_signal(self, image, guidance, key_label):
        dimensions = 3 if len(image.shape) > 3 else 2
        assert type(guidance) == torch.Tensor or type(guidance) == MetaTensor, f"guidance is {type(guidance)}, value {guidance}"

        if self.gdt or self.edt:
            assert self.disks

        if guidance.size()[0]:
            first_point_size = guidance[0].numel()
            if dimensions == 3:
                # Assume channel is first and depth is last CHWD
                assert first_point_size == 4, f"first_point_size is {first_point_size}, first_point is {guidance[0]}"
                signal = torch.zeros((1, image.shape[-3], image.shape[-2], image.shape[-1]), device=self.device)
            else:
                assert first_point_size == 3, f"first_point_size is {first_point_size}, first_point is {guidance[0]}"
                signal = torch.zeros((1, image.shape[-2], image.shape[-1]), device=self.device)

            sshape = signal.shape

            for point in guidance:
                if torch.any(point < 0):
                    continue
                if dimensions == 3:
                    # Making sure points fall inside the image dimension
                    p1 = max(0, min(int(point[-3]), sshape[-3] - 1))
                    p2 = max(0, min(int(point[-2]), sshape[-2] - 1))
                    p3 = max(0, min(int(point[-1]), sshape[-1] - 1))
                    signal[:, p1, p2, p3] = 1.0
                else:
                    p1 = max(0, min(int(point[-2]), sshape[-2] - 1))
                    p2 = max(0, min(int(point[-1]), sshape[-1] - 1))
                    signal[:, p1, p2] = 1.0
            
            # Apply a Gaussian filter to the signal
            if torch.max(signal[0]) > 0:
                signal_tensor = signal[0]
                if self.sigma != 0:
                    pt_gaussian = GaussianFilter(len(signal_tensor.shape), sigma=self.sigma)
                    signal_tensor = pt_gaussian(signal_tensor.unsqueeze(0).unsqueeze(0))
                    signal_tensor = signal_tensor.squeeze(0).squeeze(0)

                signal[0] = signal_tensor
                signal[0] = (signal[0] - torch.min(signal[0])) / (torch.max(signal[0]) - torch.min(signal[0]))
                if self.disks:
                    signal[0] = (signal[0] > 0.1) * 1.0 # 0.1 with sigma=1 --> radius = 3, otherwise it is a cube

                    if self.gdt or self.edt or self.adaptive_sigma:
                        raise UserWarning("Code no longer active")
                        # fact = 1.0 if (self.gdt or self.exp_geos or self.adaptive_sigma) else 0.0
                        # spacing  = self.spacing
                        # geos = generalised_geodesic3d(image.unsqueeze(0).to(self.device),
                        #                             signal[0].unsqueeze(0).unsqueeze(0).to(self.device),
                        #                             spacing,
                        #                             10e10,
                        #                             fact,
                        #                             4)
                        # if torch.max(geos.cpu()) > 0:
                        #     geos = (geos - torch.min(geos)) / (torch.max(geos) - torch.min(geos))
                        # vals = geos[0][0].cpu().detach().numpy()

                        # if len(vals[vals > 0]) == 0:
                        #     theta = 0
                        # else:
                        #     theta = np.percentile(vals[vals > 0], self.gdt_th)
                        # geos *= ((geos > theta) * 1.0)

                        # if self.exp_geos: # Eponentialized Geodesic Distance (MIDeepSeg)
                        #     geos = 1.0 - torch.exp(-geos)
                        # signal[0] = geos[0][0]

            if not (torch.min(signal[0]).item() >= 0 and torch.max(signal[0]).item() <= 1.0):
                raise UserWarning('[WARNING] Bad signal values', torch.min(signal[0]), torch.max(signal[0]))
            if signal is None:
                raise UserWarning("[ERROR] Signal is None")
            return signal
        else:
            if dimensions == 3:
                signal = torch.zeros((1, image.shape[-3], image.shape[-2], image.shape[-1]), device=self.device)
            else:
                signal = torch.zeros((1, image.shape[-2], image.shape[-1]), device=self.device)
            if signal is None:
                print("[ERROR] Signal is None")
            return signal

    @timeit
    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        d: Dict = dict(data)
        for key in self.key_iterator(d):
            if key == "image":
                image = d[key]
                assert image.is_cuda
                tmp_image = image[0 : 0 + self.number_intensity_ch, ...]

                guidance = d[self.guidance_key]
                # e.g. {'spleen': '[[1, 202, 190, 192], [2, 224, 212, 192], [1, 242, 202, 192], [1, 256, 184, 192], [2.0, 258, 198, 118]]', 
                # 'background': '[[257, 0, 98, 118], [1.0, 223, 303, 86]]'}

                for key_label in guidance.keys():
                    # Getting signal based on guidance
                    assert type(guidance[key_label]) == torch.Tensor or type(guidance[key_label]) == MetaTensor, f"guidance[key_label]: {type(guidance[key_label])}\n{guidance[key_label]}"
                    # logger.info(f"guidance[key_label] {key_label}: {guidance[key_label]}")
                    if guidance[key_label] is not None and guidance[key_label].numel():
                        signal = self._get_corrective_signal(image, guidance[key_label].to(device=self.device), key_label=key_label)
                    else:
                        # TODO can speed this up here
                        signal = self._get_corrective_signal(image, torch.Tensor([]).to(device=self.device), key_label=key_label)
                    assert signal.is_cuda
                    assert tmp_image.is_cuda
                    tmp_image = torch.cat([tmp_image, signal], dim=0)
                    if isinstance(d[key], MetaTensor):
                        d[key].array = tmp_image
                    else:
                        d[key] = tmp_image
                return d
            else:
                raise UserWarning("This transform only applies to image key")
        raise UserWarning("image key has not been been found")


class FindDiscrepancyRegionsDeepEditd(MapTransform):
    """
    Find discrepancy between prediction and actual during click interactions during training.

    Args:
        pred_key: key to prediction source.
        discrepancy_key: key to store discrepancies found between label and prediction.
    """

    def __init__(
        self,
        keys: KeysCollection,
        pred_key: str = "pred",
        discrepancy_key: str = "discrepancy",
        allow_missing_keys: bool = False,
        device = None
    ):
        super().__init__(keys, allow_missing_keys)
        self.pred_key = pred_key
        self.discrepancy_key = discrepancy_key
        self.device = device

    def disparity(self, label, pred):        
        disparity = label - pred
        # +1 means predicted label is not part of the ground truth
        # -1 means predicted label missed that region of the ground truth
        pos_disparity = (disparity > 0).to(dtype=torch.float32, device=self.device) # FN
        neg_disparity = (disparity < 0).to(dtype=torch.float32, device=self.device) # FP
        return [pos_disparity, neg_disparity]

    def _apply(self, label, pred):
        return self.disparity(label, pred)

    @timeit
    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        # d: Dict = dict(data)
        d = data
        for key in self.key_iterator(d):
            if key == "label":
                assert type(d[key]) == torch.Tensor and type(d[self.pred_key]) == torch.Tensor, "{}{}".format(type(d[key]), type(d[self.pred_key]))
                all_discrepancies = {}
                assert d[key].is_cuda and d["pred"].is_cuda

                # label_names: e.g. [('spleen', 1), ('background', 0)]
                for _, (label_key, label_value) in enumerate(d["label_names"].items()):
                    if label_key != "background":
                        label = torch.clone(d[key].detach())
                        # Label should be represented in 1
                        label[label != label_value] = 0
                        label = (label > 0.5).to(dtype=torch.float32)

                        # Taking single prediction
                        pred = torch.clone(d[self.pred_key].detach())
                        pred[pred != label_value] = 0
                        # Prediction should be represented in one
                        pred = (pred > 0.5).to(dtype=torch.float32)
                    else:
                        # TODO look into thos weird conversion - are they necessary?
                        # Taking single label
                        label = torch.clone(d[key].detach())
                        label[label != label_value] = 1
                        label = 1 - label
                        # Label should be represented in 1
                        label = (label > 0.5).to(dtype=torch.float32)
                        # Taking single prediction
                        pred = torch.clone(d[self.pred_key].detach())
                        pred[pred != label_value] = 1
                        pred = 1 - pred
                        # Prediction should be represented in one
                        pred = (pred > 0.5).to(dtype=torch.float32)
                    all_discrepancies[label_key] = self._apply(label, pred)
                d[self.discrepancy_key] = all_discrepancies
                return d
            else:
                logger.error("This transform only applies to 'label' key")
        raise UserWarning


class AddRandomGuidanceDeepEditd(Randomizable, MapTransform):
    """
    Add random guidance based on discrepancies that were found between label and prediction.

    Args:
        guidance_key: key to guidance source, shape (2, N, # of dim)
        discrepancy_key: key to discrepancy map between label and prediction shape (2, C, H, W, D) or (2, C, H, W)
        probability_key: key to click/interaction probability, shape (1)
        device: device the transforms shall be executed on
    """

    def __init__(
        self,
        keys: KeysCollection,
        guidance_key: str = "guidance",
        discrepancy_key: str = "discrepancy",
        probability_key: str = "probability",
        allow_missing_keys: bool = False,
        device=None,
        click_generation_strategy_key: str = "click_generation_strategy",
        patch_size: List[int] = [128,128,128]
    ):
        super().__init__(keys, allow_missing_keys)
        self.guidance_key = guidance_key
        self.discrepancy_key = discrepancy_key
        self.probability_key = probability_key
        self._will_interact = None
        # self.is_pos = None
        self.is_other = None
        self.default_guidance = None
        # self.guidance: Dict[str, List[List[int]]] = {}
        self.device = device
        self.click_generation_strategy_key = click_generation_strategy_key
        self.patch_size = patch_size

    def randomize(self, data: Mapping[Hashable, torch.Tensor]):
        probability = data[self.probability_key]
        self._will_interact = self.R.choice([True, False], p=[probability, 1.0 - probability])

    def find_guidance(self, discrepancy) -> List[int | List[int]] | None:
        assert discrepancy.is_cuda
        # discrepancy = discrepancy.to(device=self.device)
        distance = get_distance_transform(discrepancy, self.device, verify_correctness=False)
        t = get_choice_from_distance_transform_cp(distance, device=self.device)
        return t

    def add_guidance_based_on_discrepancy(self, data: Dict, guidance: torch.Tensor, key_label: str, coordinates: torch.Tensor | None=None) -> torch.Tensor:
        assert guidance.dtype == torch.int32
        # Positive clicks of the segment in the iteration
        discrepancy = data[self.discrepancy_key][key_label]
        pos_discr = discrepancy[0] # idx 0 is positive discrepancy and idx 1 is negative discrepancy

        if coordinates is None:
            # Add guidance to the current key label
            if torch.sum(pos_discr) > 0:
                tmp_gui = self.find_guidance(pos_discr)
                self.check_guidance_length(data, tmp_gui)
                if tmp_gui is not None:
                    guidance = torch.cat((guidance, torch.tensor([tmp_gui], dtype=torch.int32, device=guidance.device)), 0)
                # self.is_pos = True
        else:
            pos_discr = get_tensor_at_coordinates(pos_discr, coordinates=coordinates)
            #print(pos_discr.shape)
            if torch.sum(pos_discr) > 0:
                # TODO Add suport for 2d
                tmp_gui = self.find_guidance(pos_discr)
                if tmp_gui is not None:
                    # print(f"Old patch coordinates: {tmp_gui}")
                    tmp_gui = get_global_coordinates_from_patch_coordinates(tmp_gui, coordinates)
                    # print(f"New global coordinates: {tmp_gui}")
                    self.check_guidance_length(data, tmp_gui)
                    guidance = torch.cat((guidance, torch.tensor([tmp_gui], dtype=torch.int32, device=guidance.device)), 0)
            # print(guidance)
            # exit(0)
        return guidance

    def add_guidance_based_on_label(self, data, guidance, label):
        assert guidance.dtype == torch.int32
        #print(label.squeeze().shape)

        # Add guidance to the current key label
        if torch.sum(label) > 0:
            # generate a random sample
            tmp_gui = get_choice_from_tensor(label, device=self.device)
            self.check_guidance_length(data, tmp_gui)
            if tmp_gui is not None:
                guidance = torch.cat((guidance, torch.tensor([tmp_gui], dtype=torch.int32, device=guidance.device)), 0)
            # self.is_pos = True
        return guidance

    def check_guidance_length(self, data, new_guidance):
        if new_guidance is None:
            return
        dimensions = 3 if len(data[CommonKeys.IMAGE].shape) > 3 else 2
        if dimensions == 3:
            assert len(new_guidance) == 4, f"len(new_guidance) is {len(new_guidance)}, new_guidance is {new_guidance}"
        else:
            assert len(new_guidance) == 3, f"len(new_guidance) is {len(new_guidance)}, new_guidance is {new_guidance}"


    @timeit
    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        # d: Dict = dict(data)
        d = data
        # Get the previously generated clicks aka guidance
        guidance = d.get(self.guidance_key, None)
        if guidance is None:
            # Initialize the guidance dict
            d[self.guidance_key] = {}
        
        click_generation_strategy = d[self.click_generation_strategy_key]
        # logger.info(f"click generation strategy is {ClickGenerationStrategy(click_generation_strategy)}")
            
        if click_generation_strategy == ClickGenerationStrategy.GLOBAL_NON_CORRECTIVE:
            # uniform random sampling on label
            for idx, (key_label, _) in enumerate(d["label_names"].items()):
                tmp_gui = d[self.guidance_key].get(key_label, torch.tensor([], dtype=torch.int32, device=self.device))
                d[self.guidance_key][key_label] = self.add_guidance_based_on_label(data, tmp_gui, d["label"].eq(idx).to(dtype=torch.int32))
        elif (click_generation_strategy == ClickGenerationStrategy.GLOBAL_CORRECTIVE or
                click_generation_strategy == ClickGenerationStrategy.DEEPGROW_GLOBAL_CORRECTIVE):
            # discrepancy = d[self.discrepancy_key]
            
            if click_generation_strategy == ClickGenerationStrategy.DEEPGROW_GLOBAL_CORRECTIVE:
                # sets self._will_interact
                self.randomize(data)
            else:
                self._will_interact = True
            
            if self._will_interact:
                # logger.info("######## CREATE NEW GUIDANCE")
                for key_label in d["label_names"].keys():
                    tmp_gui = d[self.guidance_key].get(key_label, torch.tensor([], dtype=torch.int32, device=self.device))
                    assert type(tmp_gui) == torch.Tensor or type(tmp_gui) == MetaTensor
                    # Filter out -1 value
                    # TODO commented this code - is it actually needed?!?!
                    # if tmp_gui.numel() > 0:
                    #     tmp_gui = tmp_gui[torch.all(tmp_gui >= 0, dim=1).nonzero()].squeeze(1)
                    #     assert tmp_gui.dim() == 2, f"tmp_gui.shape()  {tmp_gui.shape}"

                    # Add guidance based on discrepancy
                    d[self.guidance_key][key_label] = self.add_guidance_based_on_discrepancy(data, tmp_gui, key_label)
        elif click_generation_strategy == ClickGenerationStrategy.PATCH_BASED_CORRECTIVE:
            # Split the data into patches of size self.patch_size
            # dimensions = 3 if len(data[CommonKeys.IMAGE].shape) > 3 else 2
            # H = W = D = None
            # if dimensions == 3:
            #     # Assuming CHWD
            #     H = math.ceil(data[CommonKeys.IMAGE].shape[-3] / self.patch_size[-3])
            #     W = math.ceil(data[CommonKeys.IMAGE].shape[-2] / self.patch_size[-2])
            #     D = math.ceil(data[CommonKeys.IMAGE].shape[-1] / self.patch_size[-1])
            #     amount_of_patches = H * W * D
            # else:
            #     H = math.ceil(data[CommonKeys.IMAGE].shape[-2] / self.patch_size[-2])
            #     W = math.ceil(data[CommonKeys.IMAGE].shape[-1] / self.patch_size[-1])
            #     amount_of_patches = H * W
            # logger.info(f"amount_of_patches for image of shape {data[CommonKeys.IMAGE].shape} is {amount_of_patches}")
            # assert amount_of_patches > 0 and amount_of_patches < 1000
            
            assert data[CommonKeys.LABEL].shape == data[CommonKeys.PRED].shape

            t = [
                Activationsd(keys="pred", softmax=True),
                AsDiscreted(
                    keys=("pred", "label"),
                    argmax=(True, False),
                    to_onehot=(len(data["label_names"]), len(data["label_names"])),
                ),
                # This transform is to check dice score per segment/label
                # SplitPredsLabeld(keys="pred"),
            ]
            post_transform = Compose(t)
            t_data = post_transform(data)

            # new_data = list(PatchIter(patch_size=self.patch_size)(data[CommonKeys.LABEL]))
            # TODO not working for 2d data yet!
            new_data = PatchIterd(keys=[CommonKeys.PRED, CommonKeys.LABEL], patch_size=self.patch_size)(t_data)
            # max_loss = -1
            # min_loss_coordinates = [(-1, -1), (-1, -1), (-1, -1)]
            # max_loss_patch = None
            # max_loss_coordinates = None

            pred_list = []
            label_list = []
            coordinate_list = []

            # now = time.time()
            for patch in new_data:
                actual_patch = patch[0]
                pred_list.append(actual_patch[CommonKeys.PRED])
                label_list.append(actual_patch[CommonKeys.LABEL])
                coordinate_list.append(actual_patch['patch_coords'])

                # print(actual_patch[CommonKeys.PRED].shape)
                # print(f"sum of pred {torch.sum(actual_patch[CommonKeys.PRED][1])}")
                # print(f"sum of label {torch.sum(actual_patch[CommonKeys.LABEL][1])}")
                # dice_loss = DiceLoss()
                # with torch.no_grad():
                #     loss = dice_loss.forward(input=actual_patch[CommonKeys.PRED].unsqueeze(0), target=actual_patch[CommonKeys.LABEL].unsqueeze(0)).item()
                #     # score = 1 - loss
                # print(f"DiceLoss: {loss}")
                # # print(f"DiceScore: {score}")
                # # score = compute_dice(y_pred=actual_patch[CommonKeys.PRED][1:], y=actual_patch[CommonKeys.LABEL][1:], include_background=False)
                # # print(f"compute_dice score: {score}")
                # logger.info(actual_patch['patch_coords'])
                # if loss > max_loss:
                #     # max_score_coordinates = [(H_min, H_max), (W_min, W_max), (D_min, D_max)]
                #     logger.info(f"New worst loss {loss} > {max_loss}")
                #     max_loss = loss
                #     max_loss_patch = actual_patch
                #     # print(max_score_patch)
                #     max_loss_coordinates = actual_patch['patch_coords']
                #     logger.info(max_loss_coordinates)
                # non_zeroes = torch.nonzero(actual_patch[CommonKeys.PRED][1], as_tuple=True)
                # non_zero_values = actual_patch[CommonKeys.PRED][1][non_zeroes]

                # print(f"Indices: {non_zeroes}")
                # print(f"Values: {non_zero_values}")

                # torch.set_printoptions(edgeitems=1, linewidth=200, profile="full")
                # print(actual_patch[CommonKeys.PRED][1])
                # print(actual_patch[CommonKeys.LABEL][1])

                # torch.set_printoptions("default")
            # print(f'loss calculation took {time.time()-now:.2f} seconds')
            
            label_stack = torch.stack(label_list, 0) 
            pred_stack = torch.stack(pred_list, 0)
            # print(f"{label_stack.shape=}")
            # now = time.time()

            # dice_metric = DiceMetric(include_background=True, reduction="none", ignore_empty=False)
            # scores = dice_metric(pred_stack, label_stack)
            # print(f"{scores=}")
            # print(f'DiceMetric calculation took {time.time()-now:.2f} seconds')
            # now = time.time()

            dice_loss = DiceLoss(include_background=True, reduction="none")
            with torch.no_grad():
                loss_per_label = dice_loss.forward(input=pred_stack, target=label_stack).squeeze()
                assert len(loss_per_label.shape) == 2
                # print(f"{loss_per_label.shape=}")
                # print(f"{loss_per_label=}")
                # 1. dim: patch number, 2. dim: number of labels, e.g. [27,2]
                max_loss_position_per_label = torch.argmax(loss_per_label, dim=0)
                assert len(max_loss_position_per_label) == len(data["label_names"])
                # print(f"{max_loss_position=}")
                # max_loss = loss[max_loss_position]
                # max_loss_coordinates = coordinate_list[max_loss_position]
                
                # print(f"max loss = {max_loss} at position {max_loss_coordinates}")
            # print(f'DiceLoss calculation took {time.time()-now:.2f} seconds')
            # discrepancy = d[self.discrepancy_key]
            # We now have the worst patches for each label, now sample clicks on them
            for idx, (key_label, _) in enumerate(d["label_names"].items()):
                patch_number = max_loss_position_per_label[idx]
                label_loss = loss_per_label[patch_number,idx]
                coordinates = coordinate_list[patch_number]
                # logger.info(f"Selected patch {idx} for label {key_label} with dice score: {label_loss} at coordinates: {coordinates}")
                
                tmp_gui = d[self.guidance_key].get(key_label, torch.tensor([], dtype=torch.int32, device=self.device))
                assert type(tmp_gui) == torch.Tensor or type(tmp_gui) == MetaTensor
                # # Add guidance based on discrepancy
                # # print(discrepancy[key_label][0].shape)
                d[self.guidance_key][key_label] = self.add_guidance_based_on_discrepancy(data, tmp_gui, key_label, coordinates)
                
            # del tmp_gui, pred_list, label_list, coordinate_list, loss_per_label, max_loss_position_per_label, new_data
            # exit(0)
            # next_item = next(new_data)
            # print(f"{type(next_item)}")
            # print(next_item)
            gc.collect() 

            # patch_list = []
            # max_score = -1
            # # max_score_patch_nr = -1
            # max_score_coordinates = [(-1, -1), (-1, -1), (-1, -1)]
            # for i in range(H):
            #     for j in range(W):
            #         for k in range(D):
            #             patch_number = (i+1)*(j+1)*(k+1)
            #             H_min = min(i * self.patch_size[-3], data[CommonKeys.IMAGE].shape[-3] - self.patch_size[-3])
            #             W_min = min(j * self.patch_size[-2], data[CommonKeys.IMAGE].shape[-3] - self.patch_size[-2])
            #             D_min = min(k * self.patch_size[-1], data[CommonKeys.IMAGE].shape[-3] - self.patch_size[-1])
            #             H_max = min((i+1) * self.patch_size[-3], data[CommonKeys.IMAGE].shape[-3])
            #             W_max = min((j+1) * self.patch_size[-2], data[CommonKeys.IMAGE].shape[-2])
            #             D_max = min((k+1) * self.patch_size[-1], data[CommonKeys.IMAGE].shape[-1])
            #             logger.info(f"patch {patch_number} is at position: ({H_min}:{H_max}, {W_min}:{W_max}, {D_min}:{D_max})")
            #             logger.info(f"shape of the patch: {data[CommonKeys.IMAGE][:,H_min:H_max,W_min:W_max,D_min:D_max].shape}")
            #             patch_list.append(data[CommonKeys.IMAGE][:,H_min:H_max,W_min:W_max,D_min:D_max])
                        
            # patch_tensor = torch.stack(patch_list)
            # score, not_nans = DiceHelper()(patch_tensor)
            # if score > max_score:
            #     max_score_coordinates = [(H_min, H_max), (W_min, W_max), (D_min, D_max)]
            #     logger.info(f"New best score {score} > {max_score} at patch {max_score_coordinates}")
            #     max_score = score
            
            # exit(0)

            # raise UserWarning("Not implemented")
        else:
            raise UserWarning("Unknown click strategy")
        

        return d


class SplitPredsLabeld(MapTransform):
    """
    Split preds and labels for individual evaluation
    """
    @timeit
    def __call__(self, data: Mapping[Hashable, torch.Tensor]) -> Mapping[Hashable, torch.Tensor]:
        # d: Dict = dict(data)
        d = data
        for key in self.key_iterator(d):
            if key == "pred":
                for idx, (key_label, _) in enumerate(d["label_names"].items()):
                    if key_label != "background":
                        d[f"pred_{key_label}"] = d[key][idx + 1, ...][None]
                        d[f"label_{key_label}"] = d["label"][idx + 1, ...][None]
            elif key != "pred":
                logger.info("This transform is only for pred key")
        return d

