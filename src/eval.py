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

# Code extension and modification by M.Sc. Zdravko Marinov, Karlsuhe Institute of Techonology #
# zdravko.marinov@kit.edu #
# Further code extension and modification by B.Sc. Matthias Hadlich, Karlsuhe Institute of Techonology #
# matthiashadlich@posteo.de #

from __future__ import annotations

import os
import pathlib
import resource
import sys
import time

import pandas as pd
import torch
from ignite.engine import Events
from monai.engines.utils import IterationEvents
from monai.utils.profiling import ProfileHandler, WorkflowProfiler

from sw_interactive_segmentation.api import oom_observer, create_supervised_evaluator
from sw_interactive_segmentation.utils.argparser import parse_args, setup_environment_and_adapt_args
from sw_interactive_segmentation.utils.tensorboard_logger import init_tensorboard_logger
from sw_interactive_segmentation.utils.helper import GPU_Thread, TerminationHandler, get_gpu_usage, handle_exception, is_docker
from sw_interactive_segmentation.data import post_process_AutoPET2_Challenge_file_list
# from monai.handlers import (
#     CheckpointLoader,
# )

logger = None


def run(args):
    for arg in vars(args):
        logger.info("USING:: {} = {}".format(arg, getattr(args, arg)))
    print("")
    device = torch.device(f"cuda:{args.gpu}")

    if args.dataset == "AutoPET2_Challenge":
        raise UserWarning("Use test.py for the challenge runs..")

    gpu_thread = GPU_Thread(1, "Track_GPU_Usage", os.path.join(args.output_dir, "usage.csv"), device)
    logger.info(f"Logging GPU usage to {args.output_dir}/usage.csv")

    (
        evaluator,
        key_val_metric,
        additional_val_metrics,
    ) = create_supervised_evaluator(args, resume_from=args.resume_from)
    val_metric_names = list(key_val_metric.keys()) + list(additional_val_metrics.keys())

    start_time = time.time()
    try:
        evaluator.run()
    except torch.cuda.OutOfMemoryError:
        oom_observer(device, None, None, None)
        logger.critical(get_gpu_usage(device, used_memory_only=False, context="ERROR"))
    except RuntimeError as e:
        if "cuDNN" in str(e):
            # Got a cuDNN error
            pass
        oom_observer(device, None, None, None)
        logger.critical(get_gpu_usage(device, used_memory_only=False, context="ERROR"))
    finally:
        logger.info("Total Validation Time {}".format(time.time() - start_time))


def main():
    global logger

    # Slurm only: Speed up the creation of temporary files
    if os.environ.get("SLURM_JOB_ID") is not None:
        tmpdir = "/local/work/mhadlich/tmp"
        os.environ["TMPDIR"] = tmpdir
        if not os.path.exists(tmpdir):
            pathlib.Path(tmpdir).mkdir(parents=True)

    rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (8 * 8192, rlimit[1]))

    sys.excepthook = handle_exception

    if not is_docker():
        torch.set_num_threads(int(os.cpu_count() / 3))  # Limit number of threads to 1/3 of resources

    args = parse_args()
    args, logger = setup_environment_and_adapt_args(args)

    run(args)


if __name__ == "__main__":
    main()