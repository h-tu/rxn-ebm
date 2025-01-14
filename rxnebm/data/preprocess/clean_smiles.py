""" This module contains functions to clean a raw dataset of reaction SMILES strings, which includes
operations such as removing atom mapping and checking for invalid molecule SMILES. There is also
a function to generate a list of SMILES strings of all unique molecules in the given dataset. 
NOTE: in rxn-ebm, a 'dataset' refers to the combination of 'train', 'valid', and 'test', (which are each called a 'phase')
e.g. USPTO_50k is a dataset.
"""
import argparse
import csv
import os
import pickle
import random
import re
import sys
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path
from typing import List, Optional, Tuple, Union

import numpy as np
import rdkit
from rdkit import Chem
from rdkit.Chem import rdChemReactions
from tqdm import tqdm

Mol = rdkit.Chem.rdchem.Mol

###################################
######### HELPER FUNCTIONS ########
###################################
def parse_args():
    parser = argparse.ArgumentParser("clean_smiles")
    # file paths
    parser.add_argument("--raw_smi_pre", help="File prefix of original raw rxn_smi csv", type=str, default="schneider50k")
    parser.add_argument("--clean_smi_pre", help="File prefix of cleaned rxn_smi pickle", type=str, default="50k_clean_rxnsmi_noreagent_allmapped")
    parser.add_argument("--raw_smi_root", help="Full path to folder containing raw rxn_smi csv", type=str)
    parser.add_argument("--clean_smi_root", help="Full path to folder that will contain cleaned rxn_smi pickle", type=str)

    # args for clean_rxn_smis_50k_all_phases
    parser.add_argument("--split_mode", help='Whether to keep rxn_smi with multiple products: "single" or "multi"', type=str, default="multi")
    parser.add_argument("--lines_to_skip", help="Number of lines to skip", type=int, default=1)
    parser.add_argument("--keep_reag", help="Whether to keep reagents in output SMILES string", type=bool, default=False)
    parser.add_argument("--keep_all_rcts", help="Whether to keep all rcts even if they don't contribute atoms to product", type=bool, default=False)
    parser.add_argument("--remove_dup_rxns", help="Whether to remove duplicate rxn_smi", type=bool, default=True)
    parser.add_argument("--remove_rct_mapping", help="Whether to remove atom map if atom in rct is not in product", type=bool, default=True)
    parser.add_argument("--remove_all_mapping", help="Whether to remove all atom map", type=bool, default=False)
    parser.add_argument("--save_idxs", help="Whether to save all bad indices to a file in same dir as clean_smi", type=bool, default=False)
    parser.add_argument("--parallelize", help="Whether to parallelize computation across all available cpus", type=bool, default=True)
    return parser.parse_args()

def remove_mapping(rxn_smi: str, keep_reagents: bool = False) -> str:
    """
    Removes all atom mapping from the reaction SMILES string

    Parameters
    ----------
    rxn_smi : str
        The reaction SMILES string whose atom mapping is to be removed
    keep_reagents : bool (Default = False)
        whether to keep the reagents in the output reaction SMILES string

    Returns
    -------
    str
        The reaction SMILES string with all atom mapping removed

    Also see: clean_rxn_smis_50k_one_phase, clean_rxn_smis_FULL_one_phase
    """
    rxn = rdChemReactions.ReactionFromSmarts(rxn_smi, useSmiles=True)
    if not keep_reagents:
        rxn.RemoveAgentTemplates()

    prods = [mol for mol in rxn.GetProducts()]
    for prod in prods:
        for atom in prod.GetAtoms():
            if atom.HasProp("molAtomMapNumber"):
                atom.ClearProp("molAtomMapNumber")

    rcts = [mol for mol in rxn.GetReactants()]
    for rct in rcts:
        for atom in rct.GetAtoms():
            if atom.HasProp("molAtomMapNumber"):
                atom.ClearProp("molAtomMapNumber")

    return rdChemReactions.ReactionToSmiles(rxn)


def move_reagents(
    mol_prod: Mol,
    reactants: List[Mol],
    original_reagents: List[str],
    keep_reagents: bool = False,
    keep_all_rcts: bool = False,
    remove_rct_mapping: bool = True,
) -> str:
    """
    Adapted func from Hanjun Dai's GLN - gln/data_process/clean_uspto.py --> get_rxn_smiles()
    to additionally keep track of reagents (reactants that don't appear in products)
    Gets rid of reactants when they don't contribute to the product

    Parameters
    ----------
    mol_prod : Mol
        product molecule
    reactants : List[Mol]
        list of reactant molecules
    original_reagents : List[str]
        list of reagents in the original reaction (from 'rcts>reagents>prods'), each element of list = 1 reagent
    keep_reagents : bool (Default = True)
        whether to keep reagents in the output SMILES string
    keep_all_rcts : bool (Default = False)
        whether to keep all reactants, regardless of whether they contribute any atoms to the product
        NOTE: GLN removes non-contributing reactants in their clean_uspto.py's main()
    remove_rct_mapping : bool (Default = True)
        whether to remove atom mapping if atom in reactant is not in product (i.e. leaving groups)
        NOTE: GLN removes these atom mapping in their clean_uspto.py's get_rxn_smiles()

    Returns
    -------
    str
        reaction SMILES string with only reactants that contribute to the product,
        the other molecules being moved to reagents (if keep_reagents is True)

    Also see: clean_rxn_smis_from_csv
    """
    prod_smi = Chem.MolToSmiles(mol_prod, True)
    prod_maps = set(re.findall(r"\:([[0-9]+)\]", prod_smi))
    reactants_smi_list = []

    reagent_smi_list = []
    if original_reagents:
        reagent_smi_list.append(original_reagents)

    for mol in reactants:
        if mol is None:
            continue

        used = False
        for a in mol.GetAtoms():
            if a.HasProp("molAtomMapNumber"):
                if a.GetProp("molAtomMapNumber") in prod_maps:
                    used = True

                # removes atom mapping if atom in reactant is not in product
                elif remove_rct_mapping:  
                    a.ClearProp("molAtomMapNumber")

        if keep_all_rcts: # keep all rcts rgdless of contribution to prod atoms
            reactants_smi_list.append(Chem.MolToSmiles(mol, True))
        elif used:
            reactants_smi_list.append(Chem.MolToSmiles(mol, True))
        else:
            reagent_smi_list.append(Chem.MolToSmiles(mol, True))

    reactants_smi = ".".join(reactants_smi_list)

    if not keep_reagents:
        return "{}>>{}".format(reactants_smi, prod_smi)

    if reagent_smi_list:
        reagents_smi = ".".join(reagent_smi_list)
    else:
        reagents_smi = ""

    return "{}>{}>{}".format(reactants_smi, reagents_smi, prod_smi)


###################################
########### USPTO_50k #############
###################################
def clean_rxn_smis_50k_one_phase(
    path_to_rxn_smis: Union[str, bytes, os.PathLike],
    lines_to_skip: int = 1,
    dataset_name: str = "50k", 
    keep_reagents: bool = False,
    keep_all_rcts: bool = False,
    remove_rct_mapping: bool = True,
    remove_all_mapping: bool = False,
):
    """
    Adapted function from Hanjun Dai's GLN: gln/data_process/clean_uspto.py --> main()
    NOTE: reads csv file twice, first time to get total line count for tqdm, second time to do the actual work
          This may not be practical with extremely large csv files

    Cleans reaction SMILES strings by removing those with:
        bad product (SMILES not parsable by rdkit)
        too small products, like 'O' (='H2O'), 'N'(='NH3'), i.e. a large reactant fails to be recorded as a product

    It also checks these, but does not remove them, since atom mapping is not needed for rxn-ebm:
        missing atom mapping (not all atoms in the product molecule have atom mapping),
        bad atom mapping (not 1:1 between reactants and products)

    Lastly, it also keeps track of duplicate, cleaned reaction SMILES strings and their indices in the original CSV file

    Parameters
    ----------
    path_to_rxn_smis : str
        full path to the CSV file containing the reaction SMILES strings
        there will be one CSV file each for train, valid and test, coordinated by clean_rxn_smis_all_phases
    lines_to_skip : int (Default = 1)
        how many header lines to skip in the CSV file
        This is 1 for USPTO_50k (schneider), but 3 for USPTO_STEREO, and 1 for USPTO_FULL (GLN)
        Unfortunately, this cannot be reliably extracted from some automatic algorithm, as every CSV file can be differently formatted
    split_mode : str (Default = 'multi')
        whether to keep and process reaction SMILES containing multiple products, or ignore them
        Choose between 'single' and 'multi'
    keep_reagents : bool (Default = False)
        whether to keep reagents in the output SMILES
    keep_all_rcts : bool (Default = False)
        whether to keep all reactants, regardless of whether they contribute any atoms to the product
        NOTE: GLN removes non-contributing reactants in their clean_uspto.py's main()
    remove_rct_mapping : bool (Default = True)
        whether to remove atom mapping if atom in reactant is not in product (i.e. leaving groups)
        NOTE: GLN removes these atom mapping in their clean_uspto.py's get_rxn_smiles()
    remove_all_mapping : bool (Default = False)
        whether to remove all atom mapping from the reaction SMILES,
        if True, remove_rct_mapping will be automatically set to True

    Returns
    -------
    clean_list : List[str]
        list of cleaned reaction SMILES strings with possible duplicates
        NOTE: for USPTO_50k from schneider50k, only 4 reaction SMILES should be removed for having too small products
    set_clean_list : List[str]
        list of cleaned reaction SMILES strings without duplicates
        this will be used if remove_dup_rxns is set to True in clean_rxn_smis_all_phases()
    bad_mapping_idxs : List[int]
        indices of reaction SMILES strings in original dataset with bad atom mapping (product atom id's do not all match reactant atom id's)
    bad_prod_idxs : List[int]
        indices of reaction SMILES strings in original dataset with bad products (not parsable by RDKit)
    too_small_idxs : List[int]
        indices of reaction SMILES strings in original dataset with too small products (product SMILES string smaller than 3 characters)
    missing_map_idxs : List[int]
        indices of reaction SMILES strings in original dataset with missing atom mapping
    dup_rxn_idxs : List[int]
        indices of reaction SMILES strings in original dataset that are duplicates of an already cleaned & extracted reaction SMILES string

    Also see: move_reagents, remove_mapping
    """
    if remove_all_mapping:
        remove_rct_mapping = True

    pt = re.compile(r":(\d+)]")
    clean_list, set_clean_list = [], set()
    bad_mapping, bad_mapping_idxs = 0, []
    bad_prod, bad_prod_idxs = 0, []
    missing_map, missing_map_idxs = 0, []
    too_small, too_small_idxs = 0, []
    dup_rxn_idxs = []
    extracted = 0 

    with open(path_to_rxn_smis, "r") as csv_file:
        total_lines = len(csv_file.readlines()) - lines_to_skip

    with open(path_to_rxn_smis, "r") as csv_file:
        reader = csv.reader(csv_file, delimiter="\t")
        for line in range(lines_to_skip):
            header = next(reader)  # skip first row of csv file  

        for i, row in enumerate(tqdm(reader, total=total_lines)):
            rxn_smi = row[0].split(",")[2]  # second column of the csv file
            all_rcts_smi, all_reag_smi, prods_smi = rxn_smi.split(">")
  

            rids = ",".join(sorted(re.findall(pt, all_rcts_smi)))
            pids = ",".join(sorted(re.findall(pt, prods_smi)))
            if (
                rids != pids
            ):  # atom mapping is not 1:1, but for rxn-ebm, this is not important
                bad_mapping += 1
                bad_mapping_idxs.append(i)

            all_rcts_mol = [Chem.MolFromSmiles(smi) for smi in all_rcts_smi.split(".")]

            for prod_smi in prods_smi.split("."):
                mol_prod = Chem.MolFromSmiles(prod_smi)
                if (
                    mol_prod is None
                ):  # rdkit is not able to parse the product, critical!
                    bad_prod += 1
                    bad_prod_idxs.append(i)
                    continue

                # check if all atoms in product have atom mapping, but for
                # rxn-ebm, this is not important
                if not all(
                    [a.HasProp("molAtomMapNumber") for a in mol_prod.GetAtoms()]
                ):
                    missing_map += 1
                    missing_map_idxs.append(i)

                rxn_smi = move_reagents(
                    mol_prod,
                    all_rcts_mol,
                    all_reag_smi,
                    keep_reagents,
                    keep_all_rcts,
                    remove_rct_mapping,
                )

                temp_rxn_smi = remove_mapping(rxn_smi, keep_reagents=keep_reagents)
                if (
                    len(temp_rxn_smi.split(">")[-1]) < 3
                ):  # check length of product SMILES string
                    too_small += 1  # e.g. 'Br', 'O', 'I'
                    too_small_idxs.append(i)
                    continue
                if remove_all_mapping:
                    rxn_smi = temp_rxn_smi

                clean_list.append(rxn_smi)
                if rxn_smi not in set_clean_list:
                    set_clean_list.add(rxn_smi)
                else:
                    dup_rxn_idxs.append(i)

                extracted += 1

        print("# clean rxn: ", len(clean_list))
        print("# unique rxn:", len(set_clean_list))
        print("bad mapping: ", bad_mapping)
        print("bad prod: ", bad_prod)
        print("too small: ", too_small)
        print("missing map: ", missing_map)
        print("raw rxn extracted: ", extracted, "\n")
        return (
            clean_list,
            list(set_clean_list),
            bad_mapping_idxs,
            bad_prod_idxs,
            too_small_idxs,
            missing_map_idxs,
            dup_rxn_idxs,
        )


def clean_rxn_smis_50k_all_phases(
    input_file_prefix: str = "schneider50k",
    output_file_prefix: str = "50k_clean_rxnsmi_noreagent_allmapped",
    input_root: Optional[Union[str, bytes, os.PathLike]] = None,
    output_root: Optional[Union[str, bytes, os.PathLike]] = None, 
    lines_to_skip: Optional[int] = 1,
    keep_reagents: bool = False,
    keep_all_rcts: bool = False,
    remove_dup_rxns: bool = True,
    remove_rct_mapping: bool = True,
    remove_all_mapping: bool = False,
    save_idxs: bool = False,
    parallelize: bool = True,
    **kwargs 
):
    """NOTE: assumes file extension is .csv. GLN uses .rsmi for USPTO_FULL, but it's actually .csv file

    Parameters
    ----------
    input_file_prefix : str (Default = 'schneider50k')
        file prefix of the original raw, unclean, reaction SMILES csv file for USPTO_50k
        this function appends phase = ['train', 'valid', 'test'] --> {raw_data_file_prefix}_{phase}.csv
    output_file_prefix : str (Default = '50k_clean_rxnsmi_noreagent_allmapped')
        file prefix of the output, cleaned reaction SMILES pickle file
        recommended format: '{dataset_name}_clean_rxnsmi_{any_tags}' example tags include 'noreagent', 'nostereo'
    input_root : Optional[Union[str, bytes, os.PathLike]] (Default = None)
        full file path to the folder containing the original raw data csv
        if None, we assume the original directory structure of rxn-ebm package, and set this to:
            path/to/rxnebm/data/original_data
    output_root : Optional[Union[str, bytes, os.PathLike]] (Default = None)
        full file path to the folder containing the output cleaned data pickle
        if None, we assume the original directory structure of rxn-ebm package, and set this to:
            path/to/rxnebm/data/cleaned_data 
    lines_to_skip : int (Default = 1)
        how many header lines to skip in the CSV file
        This is 1 for USPTO_50k (schneider), but 3 for USPTO_STEREO, and 1 for USPTO_FULL (GLN)
        Unfortunately, this cannot be reliably extracted from some automatic algorithm, as every CSV file can be differently formatted
    keep_reagents : bool (Default = False)
        whether to keep reagents in the output SMILES string
    keep_all_rcts : bool (Default = False)
        whether to keep all reactants, regardless of whether they contribute any atoms to the product
        NOTE: GLN removes non-contributing reactants in their clean_uspto.py's main()
    remove_dup_rxns : bool (Default = True)
        whether to remove duplicate rxn_smi
    remove_rct_mapping : bool (Default = True)
        whether to remove atom mapping if atom in reactant is not in product (i.e. leaving groups)
        NOTE: GLN removes these atom mapping in their clean_uspto.py's get_rxn_smiles()
    remove_all_mapping : bool (Default = False)
        whether to remove all atom mapping from the reaction SMILES,
        if True, remove_rct_mapping will be automatically set to True
    save_idxs : bool (Default = True)
        whether to save all bad idxs to a file in the same directory as the output file
    parallelize : bool (Default = True)
        whether to parallelize the computation across all possible workers

    Also see: clean_rxn_smis_50k_one_phase
    """
    if remove_all_mapping:
        remove_rct_mapping = True

    if input_root is None:
        input_root = Path(__file__).resolve().parents[2] / "data" / "original_data"
    if output_root is None:
        output_root = Path(__file__).resolve().parents[2] / "data" / "cleaned_data"

    phase_to_compute = ["train", "valid", "test"]
    for phase in ["train", "valid", "test"]:
        if (output_root / f"{output_file_prefix}_{phase}.pickle").exists():
            print(f"At: {output_root / output_file_prefix}_{phase}.pickle")
            print("The cleaned_rxn_smi file already exists!")
            phase_to_compute.remove(phase)
    if len(phase_to_compute) == 0:
        return

    if parallelize:
        try:
            num_workers = len(os.sched_getaffinity(0))
        except AttributeError:
            num_workers = os.cpu_count()
        print(f"Parallelizing over {num_workers} cores")
    else:
        print(f"Not parallelizing!")
        num_workers = 1

    cleaned_rxn_smis = {"train": None, "valid": None, "test": None}
    for phase in phase_to_compute:
        print(f"Processing {phase}")

        with Pool(max_workers=num_workers) as client:
            future = client.submit(
                clean_rxn_smis_50k_one_phase,
                input_root / f"{input_file_prefix}_{phase}.csv", 
                lines_to_skip=lines_to_skip,
                keep_reagents=keep_reagents,
                keep_all_rcts=keep_all_rcts,
                remove_rct_mapping=remove_rct_mapping,
                remove_all_mapping=remove_all_mapping,
            )
            if remove_dup_rxns:  # [1] is set_clean_list
                cleaned_rxn_smis[phase] = future.result()[1]
            else:
                cleaned_rxn_smis[phase] = future.result()[0]

        if save_idxs:
            bad_idxs = {}
            bad_idxs["bad_mapping_idxs"] = future.result()[2]
            bad_idxs["bad_prod_idxs"] = future.result()[3]
            bad_idxs["too_small_idxs"] = future.result()[4]
            bad_idxs["missing_map_idxs"] = future.result()[5]
            bad_idxs["dup_rxn_idxs"] = future.result()[6]

            with open(
                output_root / f"{output_file_prefix}_{phase}_badidxs.pickle", "wb"
            ) as handle:
                pickle.dump(bad_idxs, handle, protocol=pickle.HIGHEST_PROTOCOL)

        with open(output_root / f"{output_file_prefix}_{phase}.pickle", "wb") as handle:
            pickle.dump(
                cleaned_rxn_smis[phase], handle, protocol=pickle.HIGHEST_PROTOCOL
            )

###################################
######### HELPER FUNCTIONS ########
###################################
def remove_overlapping_rxn_smis(
    rxn_smi_file_prefix: str = "50k_clean_rxnsmi_noreagent_allmapped",
    root: Optional[Union[str, bytes, os.PathLike]] = None,
    save_idxs: bool = False,
):
    """ Removes rxn_smi from train that overlap with valid/test, 
    and rxn_smi from valid that overlap with test
    Has option to save indices of the removed rxn_smi as a separate pickle file for traceability 
    """
    if root is None:
        root = Path(__file__).resolve().parents[2] / "data" / "cleaned_data"
    else:
        root = Path(root)
    if (
        root / f'{rxn_smi_file_prefix}_overlap_idxs.pickle'
    ).exists():
        print(f"At: {root / f'{rxn_smi_file_prefix}_overlap_idxs.pickle'}")
        print("The overlap_idxs file already exists!")
        return

    rxn_smi_phases = {'train': None, 'valid': None, 'test': None} 
    rxn_smi_phases_set = {'train': None, 'valid': None, 'test': None}
    for phase in rxn_smi_phases:
        with open(root / f'{rxn_smi_file_prefix}_{phase}.pickle', 'rb') as handle:
            rxn_smi_phases[phase] = pickle.load(handle)
            rxn_smi_phases_set[phase] = set(rxn_smi_phases[phase]) # speed up membership testing 
             
    overlap_idxs = {'test_in_train': [], 'test_in_valid': [], 'valid_in_train': []}
    for i, rxn_smi in enumerate(tqdm(rxn_smi_phases['test'])):
        if rxn_smi in rxn_smi_phases_set['train']:
            overlap_idx = rxn_smi_phases['train'].index(rxn_smi)
            overlap_idxs['test_in_train'].append(overlap_idx)
            rxn_smi_phases['train'].pop(overlap_idx) 

        elif rxn_smi in rxn_smi_phases_set['valid']:
            overlap_idx = rxn_smi_phases['valid'].index(rxn_smi)
            overlap_idxs['test_in_valid'].append(overlap_idx)
            rxn_smi_phases['valid'].pop(overlap_idx) 

    for i, rxn_smi in enumerate(tqdm(rxn_smi_phases['valid'])):
        if rxn_smi in rxn_smi_phases_set['train']:
            overlap_idx = rxn_smi_phases['train'].index(rxn_smi)
            overlap_idxs['valid_in_train'].append(overlap_idx)
            rxn_smi_phases['train'].pop(overlap_idx) 

    for key, value in overlap_idxs.items():
        print(key, len(value))
    for phase in rxn_smi_phases:
        print(phase, len(rxn_smi_phases[phase]))
        with open(root / f'{rxn_smi_file_prefix}_{phase}.pickle', 'wb') as handle:
            pickle.dump(rxn_smi_phases[phase], handle)
    if save_idxs:
        with open(root / f'{rxn_smi_file_prefix}_overlap_idxs.pickle', 'wb') as handle:
            pickle.dump(overlap_idxs, handle)

if __name__ == "__main__":
    args = parse_args()
    if args.clean_smi_root:
        print(f"Making dir {args.clean_smi_root}")
        os.makedirs(args.clean_smi_root, exist_ok=True)

    clean_rxn_smis_50k_all_phases(
        args.raw_smi_pre,
        args.clean_smi_pre, # '50k_clean_rxnsmi_noreagent_allmapped',   
        lines_to_skip=args.lines_to_skip,   
        keep_all_rcts=args.keep_all_rcts,  #  
        remove_dup_rxns=args.remove_dup_rxns,   # 
        remove_rct_mapping=args.remove_rct_mapping, # 
        remove_all_mapping=args.remove_all_mapping #
    )  
    remove_overlapping_rxn_smis(
        rxn_smi_file_prefix=args.clean_smi_pre, #'50k_clean_rxnsmi_noreagent_allmapped', 
        root=args.clean_smi_root,
    )
