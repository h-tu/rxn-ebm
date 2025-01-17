import contextlib
import os
import pickle
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Union

import joblib
import numpy as np
import pandas as pd
import rdkit.Chem as Chem
import rdkit.Chem.AllChem as AllChem
from joblib import Parallel, delayed
from rdchiral.main import rdchiralReactants, rdchiralReaction, rdchiralRun
from rdkit import DataStructs
from rxnebm.proposer.RetroSim.retrosim.data.get_data import (
    get_data_df, split_data_df)
from rxnebm.proposer.RetroSim.retrosim.utils.generate_retro_templates import \
    process_an_example
from tqdm import tqdm

@contextlib.contextmanager
def tqdm_joblib(tqdm_object):
    # https://stackoverflow.com/questions/24983493/tracking-progress-of-joblib-parallel-execution
    """Context manager to patch joblib to report into tqdm progress bar given as argument"""
    class TqdmBatchCompletionCallback(joblib.parallel.BatchCompletionCallBack):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        def __call__(self, *args, **kwargs):
            tqdm_object.update(n=self.batch_size)
            return super().__call__(*args, **kwargs)

    old_batch_callback = joblib.parallel.BatchCompletionCallBack
    joblib.parallel.BatchCompletionCallBack = TqdmBatchCompletionCallback
    try:
        yield tqdm_object
    finally:
        joblib.parallel.BatchCompletionCallBack = old_batch_callback
        tqdm_object.close()  

# wrappers for multiprocessing
def mol_from_smiles(smiles):
    mol = Chem.MolFromSmiles(smiles)
    return mol

def mol_to_smiles(mol, isomericSmiles=True):
    smi = Chem.MolToSmiles(mol, isomericSmiles)
    return smi

def similarity_metric(fp, list_fps):
    result = DataStructs.BulkTanimotoSimilarity(fp, list_fps)
    return result

def rdchiralreactant_dist(smi):
    return rdchiralReactants(smi)

def rdchiralreaction_dist(template):
    return rdchiralReaction(template)

def rdchiralrun_dist(rxn, rct, combine_enantiomers):
    return rdchiralRun(rxn, rct, combine_enantiomers=combine_enantiomers)

class Retrosim:
    '''
    Wrapper over retrosim for preparing training corpus + fingerprints, 
    and generating one-step precursor proposals for EBM re-ranking task
    Called by rxnebm.proposer.retrosim_proposer (self.build_model & self.propose methods)

    Parameters
    ----------
    topk : int (Default = 200)
        for each product, how many of all the total proposals generated by Retrosim to be extracted
        the original Retrosim paper uses 50 (testlimit variable)
    max_prec: int (Default = 200)
        for each product, how many similar products Retrosim should consider in the product similarity search 
        the original Retrosim paper uses 100 (max_prec variable)
    similarity_type : Optional[str] (Default = 'Tanimoto') 
        ['Tanimoto', 'Dice', 'TverskyA', 'TverskyB']
        metric to use for similarity search of product fingerprints 
    fp_type : Optional[str] (Default = 'Morgan2Feat') 
        ['Morgan2noFeat', 'Morgan3noFeat', 'Morgan2Feat', 'Morgan3Feat']
        fingerprint type to generate for each product molecule
    input_data_folder : Optional[Union[str, bytes, os.PathLike]] (Default = None)
        path to the folder containing the train/valid/test reaction SMILES strings 
        if None, this defaults to:   path/to/rxn/ebm/data/cleaned_data/ 
    input_data_file_prefix : Optional[str] (Default = '50k_clean_rxnsmi_noreagent_allmapped')
        prefix of the 3 pickle files containing the train/valid/test reaction SMILES strings
    output_folder : Optional[Union[str, bytes, os.PathLike]] (Default = None)
        path to the folder that will contain the CSV file(s) containing Retrosim's proposals 
        if None, this defaults to the same folder as input_data_folder
    parallelize : Optional[bool] (Default = False)
        whether to parallelize the proposal generation step, using all available cores
    '''
    def __init__(self, 
                topk: int = 200,
                max_prec: int = 200, 
                similarity_type: Optional[str] = 'Tanimoto',
                fp_type: Optional[str] = 'Morgan2Feat', 
                input_data_folder: Optional[Union[str, bytes, os.PathLike]] = None,
                input_data_file_prefix: Optional[str] = '50k_clean_rxnsmi_noreagent_allmapped',
                output_folder: Optional[Union[str, bytes, os.PathLike]] = None,
                parallelize: Optional[bool] = True):    
        self.topk = topk
        self.max_prec = max_prec

        if similarity_type == 'Tanimoto':
            self.similarity_metric = lambda x, y: DataStructs.BulkTanimotoSimilarity(x, y)
        elif similarity_type == 'Dice':
            self.similarity_metric = lambda x, y: DataStructs.BulkDiceSimilarity(x, y)
        elif similarity_type == 'TverskyA': # weighted towards punishing only A
            self.similarity_metric = lambda x, y: DataStructs.BulkTverskySimilarity(x, y, 1.5, 1.0)
        elif similarity_type == 'TverskyB': # weighted towards punishing only B
            self.similarity_metric = lambda x, y: DataStructs.BulkTverskySimilarity(x, y, 1.0, 1.5)
        else:
            raise ValueError('Unknown similarity type')

        if fp_type == 'Morgan2Feat':
            self.getfp = lambda smi: AllChem.GetMorganFingerprint(Chem.MolFromSmiles(smi), 2, useFeatures=True)
        elif fp_type == 'Morgan2noFeat':
            self.getfp = lambda smi: AllChem.GetMorganFingerprint(Chem.MolFromSmiles(smi), 2, useFeatures=False)
        elif fp_type == 'Morgan3Feat':
            self.getfp = lambda smi: AllChem.GetMorganFingerprint(Chem.MolFromSmiles(smi), 3, useFeatures=True)
        elif fp_type == 'Morgan3noFeat':
            self.getfp = lambda smi: AllChem.GetMorganFingerprint(Chem.MolFromSmiles(smi), 3, useFeatures=False)
        else:
            raise ValueError('Unknown fingerprint type')
        
        if input_data_folder is None:
            self.input_folder = Path(__file__).resolve().parents[1] / 'data/cleaned_data/' 
        else:
            self.input_folder = Path(input_data_folder)
        self.input_prefix = input_data_file_prefix

        if output_folder is None:
            self.output_folder = self.input_folder
        else:
            self.output_folder = Path(output_folder)

        self.parallelize = parallelize

        self._prep_training_corpus() 

    def _prep_training_corpus(self) -> None:
        ''' 
        Sets (but only with training data):
            self.clean_50k (Dict[str, List[str]]), self.clean_50k_remove_atom_map (Dict[str, List[str]]), 
            self.prod_smiles (Dict[str, List[str]]), self.rcts_smiles (Dict[str, List[str]]), self.all_prod_smiles (List[str]) 
        '''
        print('Preparing training data for Retrosim...')
        clean_50k, clean_50k_remove_atom_map = {}, {}
        prod_smiles, rcts_smiles = {}, {}

        phase_marker = [] # to create 'dataset' column for dataframe 
        all_prod_smiles, all_rxn_smiles = [], [] # for dataframe 

        self.phases = ['train']
        for phase in self.phases:
            phase_rxn_smi_remove_atom_map, phase_prod_smis, phase_rcts_smis = [], [], []
            
            with open(self.input_folder / f'{self.input_prefix}_{phase}.pickle', 'rb') as handle:
                clean_50k[phase] = pickle.load(handle)
                phase_marker.extend([phase] * len(clean_50k[phase]))
                
            for rxn_smi in tqdm(clean_50k[phase]):
                all_rxn_smiles.append(rxn_smi)
                    
                prod_smi = rxn_smi.split('>>')[-1]
                prod_mol = Chem.MolFromSmiles(prod_smi)
                [atom.ClearProp('molAtomMapNumber') for atom in prod_mol.GetAtoms()]
                prod_smi_remove_atom_map = Chem.MolToSmiles(prod_mol, True)
                prod_smi_remove_atom_map = Chem.MolToSmiles(Chem.MolFromSmiles(prod_smi_remove_atom_map), True)

                all_prod_smiles.append(prod_smi_remove_atom_map)
                phase_prod_smis.append(prod_smi_remove_atom_map)
                
                rcts_smi = rxn_smi.split('>>')[0]
                rcts_mol = Chem.MolFromSmiles(rcts_smi)
                [atom.ClearProp('molAtomMapNumber') for atom in rcts_mol.GetAtoms()]
                rcts_smi_remove_atom_map = Chem.MolToSmiles(rcts_mol, True)
                # Sometimes stereochem takes another canonicalization...
                rcts_smi_remove_atom_map = Chem.MolToSmiles(Chem.MolFromSmiles(rcts_smi_remove_atom_map), True)
                phase_rcts_smis.append(rcts_smi_remove_atom_map)
                
                rxn_smi_remove_atom_map = rcts_smi_remove_atom_map + '>>' + prod_smi_remove_atom_map
                phase_rxn_smi_remove_atom_map.append(rxn_smi_remove_atom_map)
                
            clean_50k_remove_atom_map[phase] = phase_rxn_smi_remove_atom_map
            prod_smiles[phase] = phase_prod_smis
            rcts_smiles[phase] = phase_rcts_smis 

        data = pd.DataFrame({
            'prod_smiles': all_prod_smiles,
            'rxn_smiles': all_rxn_smiles,
            'dataset': phase_marker,
        })

        try:
            if prev_FP != self.getfp:
                raise NameError
        except NameError:
            all_fps = []
            for smi in tqdm(data['prod_smiles'], desc='Generating fingerprints'):
                all_fps.append(self.getfp(smi))
            data['prod_fp'] = all_fps
            prev_FP = self.getfp

        self.datasub = data.loc[data['dataset'] == 'train']
        fps = list(self.datasub['prod_fp'])
        print(f'Size of training corpus: {len(fps)}')

        try:
            with open(self.input_folder / 'jx_cache.pickle', 'rb') as handle:
                self.jx_cache = pickle.load(handle)
        except:
            print('Did not find jx_cache.pickle, initialising new jx_cache dictionary')
            self.jx_cache = {} 

        self.clean_50k = clean_50k
        self.clean_50k_remove_atom_map = clean_50k_remove_atom_map
        self.prod_smiles = prod_smiles
        self.rcts_smiles = rcts_smiles
        self.all_prod_smiles = all_prod_smiles

    def prep_valid_and_test_data(self,
                                input_data_folder: Optional[Union[str, bytes, os.PathLike]] = None, 
                                input_data_file_prefix: Optional[str] = None) -> None:
        ''' 
        Needs self._prep_training_corpus() to have executed first! 
        Sets:
            self.clean_50k (Dict[str, List[str]]), self.clean_50k_remove_atom_map (Dict[str, List[str]]), 
            self.prod_smiles (Dict[str, List[str]]), self.rcts_smiles (Dict[str, List[str]]), self.all_prod_smiles (List[str]) 
        '''           
        # retrieve existing data prepared by self._prep_training_corpus() 
        clean_50k, clean_50k_remove_atom_map = self.clean_50k, self.clean_50k_remove_atom_map
        prod_smiles, rcts_smiles = self.prod_smiles, self.rcts_smiles
        all_prod_smiles = self.all_prod_smiles # for self.propose_all() 

        print('Preparing validation and testing data for Retrosim...')
        self.phases = ['valid', 'test']
        for phase in self.phases:
            phase_rxn_smi_remove_atom_map, phase_prod_smis, phase_rcts_smis = [], [], []
            
            if input_data_folder is None:
                input_data_folder = self.input_folder
            if input_data_file_prefix is None:
                input_data_file_prefix = self.input_prefix
            with open(input_data_folder / f'{input_data_file_prefix}_{phase}.pickle', 'rb') as handle:
                clean_50k[phase] = pickle.load(handle) 

            for rxn_smi in tqdm(clean_50k[phase], desc=f'Processing {phase}'):                     
                prod_smi = rxn_smi.split('>>')[-1]
                prod_mol = Chem.MolFromSmiles(prod_smi)
                [atom.ClearProp('molAtomMapNumber') for atom in prod_mol.GetAtoms()]
                prod_smi_remove_atom_map = Chem.MolToSmiles(prod_mol, True)
                prod_smi_remove_atom_map = Chem.MolToSmiles(Chem.MolFromSmiles(prod_smi_remove_atom_map), True)
                
                all_prod_smiles.append(prod_smi_remove_atom_map)
                phase_prod_smis.append(prod_smi_remove_atom_map)
                
                rcts_smi = rxn_smi.split('>>')[0]
                rcts_mol = Chem.MolFromSmiles(rcts_smi)
                [atom.ClearProp('molAtomMapNumber') for atom in rcts_mol.GetAtoms()]
                rcts_smi_remove_atom_map = Chem.MolToSmiles(rcts_mol, True)
                # Sometimes stereochem takes another canonicalization...
                rcts_smi_remove_atom_map = Chem.MolToSmiles(Chem.MolFromSmiles(rcts_smi_remove_atom_map), True)
                phase_rcts_smis.append(rcts_smi_remove_atom_map)
                
                rxn_smi_remove_atom_map = rcts_smi_remove_atom_map + '>>' + prod_smi_remove_atom_map
                phase_rxn_smi_remove_atom_map.append(rxn_smi_remove_atom_map)
                
            clean_50k_remove_atom_map[phase] = phase_rxn_smi_remove_atom_map
            prod_smiles[phase] = phase_prod_smis
            rcts_smiles[phase] = phase_rcts_smis 

        self.clean_50k = clean_50k
        self.clean_50k_remove_atom_map = clean_50k_remove_atom_map
        self.prod_smiles = prod_smiles
        self.rcts_smiles = rcts_smiles
        self.all_prod_smiles = all_prod_smiles
        
    def propose_one(self, 
                    prod_smiles: str, 
                    topk: int = 200, 
                    max_prec: int = 200) -> List[str]:        
        ex = mol_from_smiles(prod_smiles)
        rct = rdchiralreactant_dist(prod_smiles)
        fp = self.getfp(prod_smiles)
        
        sims = self.similarity_metric(fp, [fp_ for fp_ in self.datasub['prod_fp']])
        js = np.argsort(sims)[::-1]
        
        # Get probability of precursors
        probs = {}
        for j in js[:max_prec]:
            jx = self.datasub.index[j]
            if jx in self.jx_cache:
                (template, rcts_ref_fp) = self.jx_cache[jx]
            else: 
                retro_canonical = process_an_example(self.datasub['rxn_smiles'][jx], super_general=True) 
                if retro_canonical is None: # cannot get template, most likely due to 'could not find consistent tetrahedral centre'
                    continue 
                template = '(' + retro_canonical.replace('>>', ')>>')
                rcts_ref_fp = self.getfp(self.datasub['rxn_smiles'][jx].split('>')[0])
                self.jx_cache[jx] = (template, rcts_ref_fp)
                
            rxn = rdchiralreaction_dist(template)
            try:
                outcomes = rdchiralrun_dist(rxn, rct, combine_enantiomers=False)
            except Exception as e:
                print(e)
                outcomes = []
                
            for precursors in outcomes:
                precursors_fp = self.getfp(precursors)
                precursors_sim = self.similarity_metric(precursors_fp, [rcts_ref_fp])[0]
                if precursors in probs:
                    probs[precursors] = max(probs[precursors], precursors_sim * sims[j])
                else:
                    probs[precursors] = precursors_sim * sims[j]

        mols = []
        found_rank = 9999
        for r, (prec, prob) in enumerate(sorted(probs.items(), key=lambda x:x[1], reverse=True)[:topk]):
            mols.append(mol_from_smiles(prec))
            
        proposed_precursors_smiles = [mol_to_smiles(x, True) for x in mols]
        return proposed_precursors_smiles

    def propose_one_helper(self, 
                        prod_smiles: str, 
                        results: dict, 
                        topk: int = 200, 
                        max_prec: int = 200) -> Dict[str, List[str]]:
        ''' wrapper over self.propose_one() to allow parallelization within self.propose_all() 
        '''
        results[prod_smiles] = self.propose_one(prod_smiles, topk=topk, max_prec=max_prec) 
        return results 
 
    def propose_all(self) -> None:
        ''' iterates through all product smiles in dataset (self.all_prod_smiles) 
        and proposes precursors for them based on self.max_prec and self.topk

        Sets self.all_proposed_smiles upon successful execution
        '''
        if (self.output_folder / 
            f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec.pickle'
            ).exists(): # file already exists
            with open(self.output_folder / 
                f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec.pickle', 'rb'
                ) as handle:
                self.all_proposed_smiles = pickle.load(handle)
            self._compile_into_csv() 
        
        else:
            all_proposed_smiles = {}
            if self.parallelize: 
                # TODO: see if this can also be checkpointed from inside self.propose_one_helper() 
                try:
                    num_workers = len(os.sched_getaffinity(0))
                except AttributeError:
                    num_workers = os.cpu_count()
                print(f"Parallelizing over {num_workers} cores")
                results = {} 
                with tqdm_joblib(tqdm(desc="Generating Retrosim's Proposals", total=len(self.all_prod_smiles))) as progress_bar:
                    output_dicts = Parallel(n_jobs=num_workers)(
                        delayed(self.propose_one_helper)(
                                prod_smi, results, self.topk, self.max_prec
                            ) 
                        for prod_smi in self.all_prod_smiles
                    )
                for output_dict in output_dicts:
                    all_proposed_smiles.update(output_dict)
            else:
                for i, prod_smi in enumerate(tqdm(self.all_prod_smiles, desc="Generating Retrosim's Proposals...")):
                    if prod_smi in all_proposed_smiles and len(all_proposed_smiles[prod_smi]) > 0:
                        continue # no need to repeat calculation 

                    all_proposed_smiles[prod_smi] = self.propose_one(prod_smi, self.topk, self.max_prec)

                    if i % 4000 == 0: # checkpoint temporary files
                        with open(self.output_folder / f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec_{i}.pickle', 'wb') as handle:
                            pickle.dump(all_proposed_smiles, handle, protocol=pickle.HIGHEST_PROTOCOL)
                    
            with open(
                    self.output_folder / 
                    f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec.pickle', 'wb'
                ) as handle:
                pickle.dump(all_proposed_smiles, handle, protocol=pickle.HIGHEST_PROTOCOL)
                
            with open(self.input_folder / 'jx_cache.pickle', 'wb') as handle:
                pickle.dump(jx_cache, handle, protocol=pickle.HIGHEST_PROTOCOL)

            self.all_proposed_smiles = all_proposed_smiles
            self._compile_into_csv() 

    def _compile_into_csv(self):
        '''
        Sets self.proposed_precursors 
        Also runs self._calc_accs() 
        '''
        if self.all_proposed_smiles is None:
            with open(
                    self.output_folder / 
                    f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec.pickle', 'rb'
                ) as handle:
                self.all_proposed_smiles = pickle.load(handle)
        
        proposed_precursors = {}
        self.phases = ['train', 'valid', 'test']
        for phase in self.phases:
            dup_count = 0
            phase_proposed_precursors = []
            for rxn_smi in self.clean_50k_remove_atom_map[phase]:
                prod_smi = rxn_smi.split('>>')[-1]
                
                precursors = self.all_proposed_smiles[prod_smi]

                # check for duplicates - but by design, retrosim shouldn't make any duplicate proposal
                seen = []
                for prec in precursors: # no need to canonicalize bcos retrosim already canonicalized
                    if prec not in seen:
                        seen.append(prec)
                    else:
                        dup_count += 1

                if len(precursors) < self.topk:
                    precursors.extend(['9999'] * (self.topk - len(precursors)))
        
                phase_proposed_precursors.append(precursors)
            proposed_precursors[phase] = phase_proposed_precursors
            dup_count /= len(self.clean_50k_remove_atom_map[phase])
            print(f'Avg # dups per product for {phase}: {dup_count}') # should be 0
        self.proposed_precursors = proposed_precursors
        print('Compiled proposed_precursors by rxn_smi!')

        self.ranks, self.accs = self._calc_accs()
        # repeat accuracy calculation after removing ground truth predictions
        _, _ = self._calc_accs()

        combined = {}
        for phase in self.phases:
            zipped = []
            for rxn_smi, prod_smi, rcts_smi, rank_of_true_precursor, proposed_rcts_smi in zip(
                self.clean_50k[phase],
                self.prod_smiles[phase],
                self.rcts_smiles[phase],
                self.ranks[phase],
                proposed_precursors[phase],
            ):
                result = []
                result.extend([rxn_smi, prod_smi, rcts_smi, rank_of_true_precursor])
                result.extend(proposed_rcts_smi)
                zipped.append(result)
                
            combined[phase] = zipped
        print('Zipped all info for each rxn_smi into a list for dataframe creation!')

        processed_dataframes = {}
        for phase in self.phases:
            temp_dataframe = pd.DataFrame(
                data={
                    'zipped': combined[phase]
                }
            )
            
            phase_dataframe = pd.DataFrame(
                temp_dataframe['zipped'].to_list(),
                index=temp_dataframe.index
            )
            if phase == 'train': # true precursor has been removed from the proposals, so whatever is left are negatives
                proposed_col_names = [f'neg_precursor_{i}' for i in range(1, self.topk+1)]
            else: # validation/testing, we don't assume true precursor is present & we also do not remove them if present
                proposed_col_names = [f'cand_precursor_{i}' for i in range(1, self.topk+1)]
             
            base_col_names = ['orig_rxn_smi', 'prod_smi', 'true_precursors', 'rank_of_true_precursor']
            base_col_names.extend(proposed_col_names)
            phase_dataframe.columns = base_col_names
            
            processed_dataframes[phase] = phase_dataframe
            print(f'Shape of {phase} dataframe: {phase_dataframe.shape}')
            
            phase_dataframe.to_csv(
                self.output_folder / 
                f'retrosim_{self.topk}maxtest_{self.max_prec}maxprec_{phase}.csv',
                index=False
            )
        print(f'Saved all proposals as 3 dataframes in {self.output_folder}!')
    
    def _calc_accs(self):
        '''
        Returns:
            ranks, accs
        '''
        ranks = {}
        for phase in self.phases:  
            phase_ranks = []
            if phase == 'train':
                for idx in tqdm(range(len(self.clean_50k[phase])), desc=phase):
                    true_precursors = self.rcts_smiles[phase][idx]
                    all_proposed_precursors = self.proposed_precursors[phase][idx]

                    found = False
                    for rank, proposal in enumerate(all_proposed_precursors): # ranks are 0-indexed 
                        if true_precursors == proposal:
                            phase_ranks.append(rank)
                            # remove true precursor from proposals 
                            all_proposed_precursors.pop(rank) 
                            all_proposed_precursors.append('9999')
                            found = True
                            break

                    if not found:
                        phase_ranks.append(9999)
                    self.proposed_precursors[phase][idx] = all_proposed_precursors
            else:
                for idx in tqdm(range(len(self.clean_50k[phase])), desc=phase):
                    true_precursors = self.rcts_smiles[phase][idx]
                    all_proposed_precursors = self.proposed_precursors[phase][idx]

                    found = False
                    for rank, proposal in enumerate(all_proposed_precursors): # ranks are 0-indexed  
                        if true_precursors == proposal:
                            phase_ranks.append(rank) 
                            # do not pop true precursor from proposals! 
                            found = True
                            break

                    if not found:
                        phase_ranks.append(9999)
                    self.proposed_precursors[phase][idx] = all_proposed_precursors
            ranks[phase] = phase_ranks
            
        accs = {}
        for phase in self.phases:
            phase_accs = []
            for n in [1, 3, 5, 10, 20, 50]:
                total = float(len(ranks[phase]))
                phase_accs.append(sum([r+1 <= n for r in ranks[phase]]) / total)        
                print(f'{phase} Top-{n} accuracy: {phase_accs[-1]*100:.3f}%')
            
            print('\n')
            accs[phase] = phase_accs

        return ranks, accs

    def analyse_proposed(self):
        if self.all_proposed_smiles is None:
            with open(
                    self.output_folder / 
                    f'retrosim_proposed_smiles_{self.topk}maxtest_{self.max_prec}maxprec.pickle', 'rb'
                ) as handle:
                self.all_proposed_smiles = pickle.load(handle)

        proposed_counter = Counter()
        total_proposed, min_proposed, max_proposed = 0, float('+inf'), float('-inf')
        key_count = 0
        for key, value in tqdm(self.all_proposed_smiles.items()):
            precursors_count = len(value)
            total_proposed += precursors_count
            if precursors_count > max_proposed:
                max_proposed = precursors_count
                prod_smi_max = key
            if precursors_count < min_proposed:
                min_proposed = precursors_count
                prod_smi_min = key
            
            proposed_counter[key] = precursors_count
            key_count += 1
            
        print(f'Average precursors proposed per prod_smi: {total_proposed / key_count}')
        print(f'Min precursors: {min_proposed} for {prod_smi_min}')
        print(f'Max precursors: {max_proposed} for {prod_smi_max})')

        print(f'\nMost common 20:')
        for i in proposed_counter.most_common(20):
            print(f'{i}')
        print(f'\nLeast common 20:')
        for i in proposed_counter.most_common()[-20:]:
            print(f'{i}')

if __name__ == '__main__': 
    retrosim_model = Retrosim(topk=200, max_prec=200, similarity_type='Tanimoto',
                            fp_type='Morgan2Feat')
    retrosim_model.prep_valid_and_test_data(input_data_file_prefix='50k_clean_rxnsmi_noreagent_allmapped')
    retrosim_model.propose_all()
    retrosim_model.analyse_proposed() 
