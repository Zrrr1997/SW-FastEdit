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

from typing import Callable, Dict, Sequence, Union
import logging
import time
import pprint
import os

import numpy as np
import torch
import nibabel as nib

from monai.data import decollate_batch, list_data_collate
from monai.engines import SupervisedEvaluator, SupervisedTrainer
from monai.engines.utils import IterationEvents
from monai.transforms import Compose, AsDiscrete
from monai.utils.enums import CommonKeys
from monai.metrics import compute_dice
from monai.data.meta_tensor import MetaTensor

from utils.helper import print_gpu_usage, get_total_size_of_all_tensors, describe_batch_data, timeit

logger = logging.getLogger("interactive_segmentation")
np.seterr(all='raise')

# To debug Nans, slows down code:
# torch.autograd.set_detect_anomaly(True)


class Interaction:
    """
    Ignite process_function used to introduce interactions (simulation of clicks) for DeepEdit Training/Evaluation.

    More details about this can be found at:

        Diaz-Pinto et al., MONAI Label: A framework for AI-assisted Interactive
        Labeling of 3D Medical Images. (2022) https://arxiv.org/abs/2203.12362

    Args:
        deepgrow_probability: probability of simulating clicks in an iteration
        transforms: execute additional transformation during every iteration (before train).
            Typically, several Tensor based transforms composed by `Compose`.
        train: True for training mode or False for evaluation mode
        click_probability_key: key to click/interaction probability
        label_names: Dict of label names
        max_interactions: maximum number of interactions per iteration
    """

    def __init__(
        self,
        deepgrow_probability: float,
        transforms: Union[Sequence[Callable], Callable],
        train: bool,
        label_names: Union[None, Dict[str, int]] = None,
        click_probability_key: str = "probability",
        max_interactions: int = 1,
        args = None,
        loss_function=None,
        post_transform=None,
    ) -> None:

        self.deepgrow_probability = deepgrow_probability
        self.transforms = Compose(transforms) if not isinstance(transforms, Compose) else transforms # click transforms

        self.train = train
        self.label_names = label_names
        self.click_probability_key = click_probability_key
        self.max_interactions = max_interactions
        self.args = args
        self.loss_function = loss_function
        self.post_transform = post_transform
        # self.state = 'train' if self.train else 'eval'

    @timeit
    def __call__(self, engine: Union[SupervisedTrainer, SupervisedEvaluator], batchdata: Dict[str, torch.Tensor]):
        if batchdata is None:
            raise ValueError("Must provide batch data for current iteration.")
        
        if not self.train:
            # Evaluation does not print epoch / iteration information
            logger.info(f"### Interaction, Epoch {engine.state.epoch}/{engine.state.max_epochs}, Iter {((engine.state.iteration - 1) % engine.state.epoch_length) + 1}/{engine.state.epoch_length}")
        print_gpu_usage(device=engine.state.device, used_memory_only=True, context="START interaction class")

        # Set up the initial batch data
        in_channels=1 + len(self.args.labels)
        batchdata_list = decollate_batch(batchdata)
        for i in range(len(batchdata_list)):
            tmp_image = batchdata_list[i][CommonKeys.IMAGE][0 : 0 + 1, ...]
            assert len(tmp_image.shape) == 4
            new_shape = list(tmp_image.shape)
            new_shape[0] = in_channels
            # Set the signal to 0 for all input images
            # image is on channel 0 of e.g. (1,128,128,128) and the signals get appended, so
            # e.g. (3,128,128,128) for two labels
            inputs = torch.zeros(new_shape, device=engine.state.device)
            inputs[0] = batchdata_list[i][CommonKeys.IMAGE][0]
            batchdata_list[i][CommonKeys.IMAGE] = inputs
        batchdata = list_data_collate(batchdata_list)
        


        if np.random.choice([True, False], p=[self.deepgrow_probability, 1 - self.deepgrow_probability]):
            before_it = time.time()
            for j in range(self.max_interactions):
                # NOTE: Image shape e.g. 3x192x192x256, label shape 1x192x192x256
                inputs, labels = engine.prepare_batch(batchdata, device=engine.state.device)
                if j == 0:
                    logger.info("inputs.shape is {}".format(inputs.shape))
                    # Make sure the signal is empty in the first iteration assertion holds
                    assert torch.sum(inputs[:,1:,...]) == 0
                    logger.info(f"image file name: {batchdata['image_meta_dict']['filename_or_obj']}")
                    logger.info(f"labe file name: {batchdata['label_meta_dict']['filename_or_obj']}")

                engine.fire_event(IterationEvents.INNER_ITERATION_STARTED)
                engine.network.eval()

                # Forward Pass
                with torch.no_grad():
                    if engine.amp:
                        with torch.cuda.amp.autocast():
                            predictions = engine.inferer(inputs, engine.network)
                    else:
                        predictions = engine.inferer(inputs, engine.network)
                
                batchdata[CommonKeys.PRED] = predictions
                
                # if not self.train or self.args.save_nifti or self.args.debug:
                loss = self.loss_function(batchdata["pred"], batchdata["label"])
                logger.info(f'It: {j} {self.loss_function.__class__.__name__}: {loss:.4f} Epoch: {engine.state.epoch}')

                if j <= 9 and self.args.save_nifti:
                    tmp_batchdata = {"pred": predictions, "label": batchdata["label"], "label_names": batchdata["label_names"]}
                    tmp_batchdata_list = decollate_batch(tmp_batchdata)
                    for i in range(len(tmp_batchdata_list)):
                        tmp_batchdata_list[i] = self.post_transform(tmp_batchdata_list[i])
                    tmp_batchdata = list_data_collate(tmp_batchdata_list)
                    
                    self.debug_viz(inputs, labels, tmp_batchdata["pred"], j)

                # decollate/collate batchdata to execute click transforms
                batchdata_list = decollate_batch(batchdata)
                for i in range(len(batchdata_list)):
                    batchdata_list[i][self.click_probability_key] = self.deepgrow_probability
                    # before = time.time()
                    batchdata_list[i] = self.transforms(batchdata_list[i]) # Apply click transform, TODO add patch sized transform

                batchdata = list_data_collate(batchdata_list)

                engine.fire_event(IterationEvents.INNER_ITERATION_COMPLETED)
            logger.info(f"Interaction took {time.time()- before_it:.2f} seconds..")

        engine.state.batch = batchdata
        return engine._iteration(engine, batchdata) # train network with the final iteration cycle

    def debug_viz(self, inputs, labels, preds, j):
        self.save_nifti(f'{self.args.data}/guidance_bgg_{j}', inputs[0,2].cpu().detach().numpy())
        self.save_nifti(f'{self.args.data}/guidance_fgg_{j}', inputs[0,1].cpu().detach().numpy())
        self.save_nifti(f'{self.args.data}/labels', labels[0,0].cpu().detach().numpy())
        self.save_nifti(f'{self.args.data}/im', inputs[0,0].cpu().detach().numpy())
        self.save_nifti(f'{self.args.data}/pred_{j}', preds[0,1].cpu().detach().numpy())
        if j == self.max_interactions:
            exit()

    def save_nifti(self, name, im):
        affine = np.eye(4)
        affine[0][0] = -1
        ni_img = nib.Nifti1Image(im, affine=affine)
        ni_img.header.get_xyzt_units()
        ni_img.to_filename(f'{name}.nii.gz')

