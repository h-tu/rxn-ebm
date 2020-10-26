# rxn-ebm
Energy-based modeling of chemical reactions

## Environmental setup
#### Using Conda
    conda create -n rxnebm python=3.8 tqdm pathlib typing scipy
    conda activate rxnebm
    conda install pytorch torchvision cudatoolkit=10.2 -c pytorch 
    # conda install pytorch torchvision cpuonly -c pytorch # to install cpuonly build of pytorch
    
    # install latest version of rdkit 
    conda install -c conda-forge rdkit 
    
    # install nmslib
    pip install nmslib
    
    # install crem
    pip install crem

## Data setup
#### Data source
The data was obtained from https://www.dropbox.com/sh/6ideflxcakrak10/AADN-TNZnuGjvwZYiLk7zvwra/schneider50k?dl=0&subfolder_nav_tracking=1, provided by the authors of GLN. 
We rename these 3 excel files to ```'schneider50k_train.csv'```, ```'schneider50k_test.csv'``` and ```'schneider50k_valid.csv'```, and save them to data/original_data <br>

#### Data preprocessing
Then, simply adjust the parameters as you wish in trainEBM.py and run the script. (Currently adding more arguments to be parsed from command-line) <br>

This will first execute ```trainEBM.prepare_data()```, which cleans the raw SMILES strings, extracts all unique molecule SMILES strings as a list, and converts them into a matrix of Morgan count molecular fingerprints. It also generates a lookup table mapping molecular SMILES strings into the corresponding index in that matrix of Morgan count fingerprints. Next, it builds a nearest-neighbour search index using the nmslib package. Lastly, it generates a (very) large database of CReM negatives, mapping each product SMILES string in the dataset (key), to a list of highly similar, mutated product SMILES strings (value). Note that this step can take from 10-13 hours on the USPTO_50k dataset for 150 mutated products / original product, and that CReM does not guarantee the requested number of mutated products. <br>

#### List of provided data:
For ease of reproducibility, the data is available at: https://drive.google.com/drive/folders/1ISXFL7SuVY_sW3z36hQfpyDMH1KF22nS?usp=sharing <br>
- three ```50k_clean_rxnsmi_noreagent_{split}.pickle``` files contain the cleaned reaction SMILES strings from USPTO_50k (generated by data/preprocess/clean_smiles.py)
- ```50k_mol_smis.pickle``` (list of all unique molecule SMILES, generated by data/preprocess/clean_smiles.py) 
- ```50k_mol_smi_to_sparse_fp_idx.pickle``` (the lookup table mapping molecular SMILES to Morgan count molecular fingerprints, generated by data/preprocess/smi_to_fp.py) 
- ```50k_count_mol_fps.npz``` (matrix of Morgan count moelcular fingerprints, generated by data/preprocess/smi_to_fp.py) 
- ```50k_cosine_count.bin``` & ```50k_cosine_count.bin.dat``` (generated by data/preprocess/prep_nmslib.py)
- ```50k_neg150_rad2_maxsize3_mutprodsmis.pickle``` (generated by data/preprocess/prep_crem.py). ```50k_neg150_rad2_maxsize3_insufficient.pickle``` is a supplementary dictionary mapping each product SMILES (key) for which CReM could not generate the requested number of mutated molecules, to the number of negatives (value) that CReM could generate. <br>

With the above data, you can simply specify the augmentations to apply, and trainEBM.py will pre-compute the positive & negative reaction fingerprints for training and testing. For additional convenience, a sample set of pre-computed reaction fingerprints has also been provided:
- ```50k_rdm_5_cos_5_bit_5_1_1_mut_10_{split}.npz```, the syntax being: ```{dataset_name}_rdm_{num_rdm_negs}_cos_{num_cos_negs}_bit_{num_bit_negs}_{num_bits}_{increment_bits}_mut_{num_mut_negs}_{split}.npz``` 

## Misc details
This project uses ``` black ``` for auto-formatting to the ``` pep8 ``` style guide, and ``` isort ``` to sort imports. ``` pylint ``` is also used in ``` vscode ``` to lint all code. 

## Folder organisation
```
 rxn-ebm
    ├── trainEBM.py
    ├── experiment
    │    ├── expt.py
    |    └── expt_utils.py
    ├── model
    |    ├── base.py
    │    ├── FF.py
    |    └── model_utils.py
    ├── data
    |    ├── dataset.py
    |    ├── augmentors.py
    |    ├── analyse_results.py
    |    ├── preprocess
    |    |        ├── clean_smiles.py    
    |    |        ├── smi_to_fp.py
    |    |        ├── prep_crem.py
    |    |        └── prep_nmslib.py
    |    ├── original_data  
    |    └── cleaned_data
    ├── checkpoints
    ├── scores
    └── notebooks
         └── data_exploration.ipynb 
 ```
