#!/usr/bin/bash

set -euf -o pipefail

SCRIPTPATH="$(dirname "$( cd "$(dirname "$0")" ; pwd -P)")"
SCRIPTPATHCURR="$( cd "$(dirname "$0")" ; pwd -P )"
SCRIPTPATH=$SCRIPTPATHCURR
echo $SCRIPTPATH

./build.sh

VOLUME_SUFFIX=$(dd if=/dev/urandom bs=32 count=1 | md5sum | cut --delimiter=' ' --fields=1)
MEM_LIMIT="15g"  # Maximum is currently 30g, configurable in your algorithm image settings on grand challenge

VOLUME=unet_baseline-output
#docker volume create $VOLUME 
#echo "Volume created, running evaluation"
#-$VOLUME_SUFFIX
VOLUME=$SCRIPTPATH/output/
# Do not change any of the parameters to docker run, these are fixed
docker run --rm \
        --memory="${MEM_LIMIT}" \
        --memory-swap="${MEM_LIMIT}" \
        --network="none" \
        --cap-drop="ALL" \
        --security-opt="no-new-privileges" \
        --gpus="all"  \
        --shm-size="128m" \
        --pids-limit="256" \
        -v /cvhci/data/AutoPET/AutoPET/:/input/ \
        -v $VOLUME:/output/ \
        sw_segmentation python src/test.py -i /input/ -d /tmp -o /output/images/automated-petct-lesion-segmentation/ --use_scale_intensity_range_percentiled --non_interactive -a --disks --gpu_size small --eval_only --limit_gpu_memory_to 0.66 --resume_from 195.pt -ta --dataset AutoPET --dont_check_output_dir --no_log --dont_crop_foreground --sw_overlap 0.25 --no_data -x 0 --val_sw_batch_size 8

echo "Evaluation done, checking results"
#docker build -f Dockerfile.eval -t unet_eval .

#python src/compute_metrics.py -l test/input/autopet_labels/ -p output/images/automated-petct-lesion-segmentation/nii/ -o evaluation/

docker run --rm \
        --memory="${MEM_LIMIT}" \
        --memory-swap="${MEM_LIMIT}" \
        --network="none" \
        --cap-drop="ALL" \
        --security-opt="no-new-privileges" \
        --gpus="all"  \
        --shm-size="128m" \
        --pids-limit="256" \
        -v /cvhci/data/AutoPET/AutoPET/:/input/ \
        -v $VOLUME:/output/ \
        sw_segmentation python src/compute_metrics.py -l /input/labelsTs -p /output/images/automated-petct-lesion-segmentation/predictions/ -o evaluation/


#docker run --rm -it \
#        -v $VOLUME:/output/ \
#        -v $SCRIPTPATH/test/expected_output_uNet/:/expected_output/ \
#        unet_eval python3 -c """
#import SimpleITK as sitk
#import os
#print('Start')
#file = os.listdir('/output/images/automated-petct-lesion-segmentation')[0]
#print(file)
#output = sitk.GetArrayFromImage(sitk.ReadImage(os.path.join('/output/images/automated-petct-lesion-segmentation/', file)))
#expected_output = sitk.GetArrayFromImage(sitk.ReadImage('/expected_output/PRED.nii.gz'))
#mse = sum(sum(sum((output - expected_output) ** 2)))
#if mse <= 10:
#    print('Test passed!')
#else:
#    print(f'Test failed! MSE={mse}')
#"""

#docker volume rm unet_baseline-output-$VOLUME_SUFFIX