# SW-FastEdit (Accepted at IEEE ISBI 2024)



Authors: Matthias Hadlich, Zdravko Marinov, Rainer Stiefelhagen
Link to the Paper https://ieeexplore.ieee.org/document/10635459 


# Sliding Window-based Interactive Segmentation of Volumetric Medical Images


**Important**: This code is only tested on 3D PET images of AutoPET(II). 2D images are not suppported, I think the code has to be adapted for that.





## Full deep learning workflow

### Training on AutoPET II

Use the `train.py` file for that. Use the `--resume_from` flag to resume training from a previous checkpoint. Example usage:

`python train.py -a -i [YOUR_PATH]/AutoPET --dataset AutoPET -o [OUTPUT_PATH]  -c [CACHE_PATH] -ta -e 400`

### Evaluation on AutoPET II

Use the `train.py` file for that and only add the `--eval_only` flag. The network will only run the evaluator which finishes after one epoch. Evaluation will use the images and the label and thus print a metric at the end.
Use the `--resume_from` flag to load previous weights.
Use `--save_pred` to save the resulting predictions.


### MONAI Label with 3D Slicer 

There are multiple steps involved to get this to run.

Optional: Create a new conda environment
1) Install monailabel via `pip install monailabel`.
2) Install the dependencies of this repository with `pip install -r requirements.txt`, then install this repository as a package via `pip install -e`. Hopefully this step can be removed in the future when the code is integrated into MONAI.
3) Download the radiology sample app `monailabel apps --download --name radiology --output .`
    (Alternative: Download the entire monailabel repo and just launch monailabel from there)
4) Copy the files from the repo under `monailabel/` to `radiology/lib/` and into the according folders `infers/` and `configs/`.
5) Download the weights from https://bwsyncandshare.kit.edu/s/Yky4x6PQbtxLj2H , rename it to `pretrained_sw_fastedit.pt` and put them into the (new) folder `radiology/model/`. This model was pretrained on tumor-only AutoPET volumes.
6) Make sure your images follow the monailabel convention, so e.g. all Nifti files in one folder `imagesTs`.

You can then run the model with (adapt the studies path where the images lie):

`monailabel start_server --app radiology --studies ../imagesTs --conf models sw_fastedit`







