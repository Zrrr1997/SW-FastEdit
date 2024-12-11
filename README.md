# SW-FastEdit (Accepted at IEEE ISBI 2024)



Authors: Matthias Hadlich*, Zdravko Marinov*, Moon Kim, Enrico Nasca, Jens Kleesiek, Rainer Stiefelhagen

Link to the Paper: https://ieeexplore.ieee.org/document/10635459 


# Sliding Window-based Interactive Segmentation of Volumetric Medical Images


**Important**: This code is only tested on 3D PET images of AutoPET II. Make sure your paths point only to PET images and not to PET/CTs


## MONAI Label with 3D Slicer 

There are a few steps you need to do and then you can start annotating! 

**Prerequisite: Please make sure you have CUDA 12.x installed!**

1) Create a new conda environment 
```
conda create -n swfastedit python=3.10 -y
conda activate swfastedit
```
2) Install monailabel
```
pip install -U monailabel
```
3) Install the dependencies of this repository with 
```
pip install -r requirements.txt
```
4) Download the radiology sample app 
```
monailabel apps --download --name radiology --output .
```
5) Make sure your images follow the monailabel convention, so e.g. all Nifti files in one folder `imagesTs`.

You can then run the model with (adapt the studies path where the images lie):

```
monailabel start_server --app radiology --studies [YOUR_PATH_TO_AUTOPET]/imagesTs --conf models sw_fastedit
```
6) Download 3D Slicer from https://download.slicer.org/
7) Install MONAI Label plugin in 3D Slicer
- Go to **View** -> **Extension Manager** -> **Active Learning** -> **MONAI Label**
- Install MONAI Label plugin
- _**Restart**_ 3D Slicer
8) Navigate to "Welcome to Slicer" dropdown menu -> Active Learning -> MONAI Label
- Click the green arrow next to the http://localhost:8000 field
- Click on "Next Sample". This will load a PET image from the specified `imagesTs` path
- Start annotating!
9) The "Update" button will infer with SW-FastEdit and all currently added clicks
10) The "Save Label" button will save the current prediction as a finished label 
11) The "Landmarks" button (arrow) allows you to place "tumor" or "background" clicks which are used to guide SWFastEdit 

## Training on AutoPET II

Use the `train.py` file for that. Use the `--resume_from` flag to resume training from a previous checkpoint. Example usage:

```
python src/train.py -a -i [YOUR_PATH]/AutoPET --dataset AutoPET -o [OUTPUT_PATH]  -c [CACHE_PATH] -ta -e 400 --dont_check_output_dir
```

The dataset structure should follow the nn-UNet format with image filenames matching the label filenames:
```
AutoPET
├── imagesTr
├── imagesTs
├── labelsTr
├── labelsTs
```
## Evaluation on AutoPET II

Use the `train.py` file for that and only add the `--eval_only` flag. The network will only run the evaluator which finishes after one epoch. Evaluation will use the images and the label and thus print a metric at the end.
Use the `--resume_from` flag to load previous weights.
Use `--save_pred` to save the resulting predictions.

```
python src/train.py -a -i [YOUR_PATH]/AutoPET --dataset AutoPET -o ./output/  -c [CACHE_PATH] -ta -e 800 --dont_check_output_dir --resume_from model/151_best_0.8534.pt --eval_only
```

To download the `151_best_0.8534.pt` checkpoint, simply use this [link](https://bwsyncandshare.kit.edu/s/Yky4x6PQbtxLj2H). 

## Contact
If you find any issues with the package installation and setup or find any major bug in the code, feel free to create and issue or to write an e-mail to zdravko.marinov@kit.edu 

## Acknowledgements
This project has grown iteratively, starting from [DeepEdit, MICCAI 2022](https://arxiv.org/abs/2305.10655), then built on top with [Guiding the Guidance, MICCAI 2023](https://link.springer.com/chapter/10.1007/978-3-031-43898-1_61), followed by the master thesis of Matthias Hadlich at [KIT](https://www.kit.edu) for it to be published in ISBI 2024 as [SW-FastEdit](https://ieeexplore.ieee.org/document/10635459 ). We thank all of the involved people who have made their code publicly available throughout the evolution of this codebase.






