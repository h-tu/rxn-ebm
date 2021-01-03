import argparse
import csv
import pickle
from tqdm import tqdm
import time

from rxnebm.data.preprocess import canonicalize

parser = argparse.ArgumentParser()
parser.add_argument('--output_format',
                    type=str,
                    default='gln',
                    help='["gln", "retroxpert", "mt"]')
# TODO: load schneider50k csv directly & clean it here instead of loading cleaned .pickle file
# TODO: add support for rxn_type
# parser.add_argument('--typed',
#                     action="store_true",
#                     help='whether using rxn_type data or not')
args = parser.parse_args()

def prep_canon_gln(remove_mapping : bool = False):
    start = time.time()
    rxn_class = "UNK"
    for phase in ['train', 'valid', 'test']:
        with open(f'rxnebm/data/cleaned_data/50k_clean_rxnsmi_noreagent_allmapped_{phase}.pickle', 'rb') as handle:
            rxn_smis = pickle.load(handle)

        with open(f'rxnebm/data/cleaned_data/clean_gln_{phase}.csv', mode='w') as f:
            writer = csv.writer(f, delimiter=',')
            # header
            writer.writerow(['id', 'class', 'reactants>reagents>production'])
            
            for i, rxn_smi in enumerate(tqdm(rxn_smis, desc=f'Writing rxn_smi in {phase}')):
                rxn_smi_canon, _, _ = canonicalize.canonicalize_rxn_smi(rxn_smi, remove_mapping=remove_mapping)
                writer.writerow([i, rxn_class, rxn_smi_canon])
            
    print(f'Finished all phases! Elapsed: {time.time() - start:.2f} secs')
    # very fast, ~60 sec for USPTO-50k

def prep_canon_retroxpert(remove_mapping : bool = False):
    start = time.time()
    rxn_class = "UNK"
    for phase in ['train', 'valid', 'test']:
        with open(f'rxnebm/data/cleaned_data/50k_clean_rxnsmi_noreagent_allmapped_{phase}.pickle', 'rb') as handle:
            rxn_smis = pickle.load(handle)

        with open(f'rxnebm/data/cleaned_data/{phase}.csv', mode='w') as f:
            writer = csv.writer(f, delimiter=',')
            # header
            writer.writerow(['class', 'id', 'rxn_smiles'])
            
            for i, rxn_smi in enumerate(tqdm(rxn_smis, desc=f'Writing rxn_smi in {phase}')):
                rxn_smi_canon, _, _ = canonicalize.canonicalize_rxn_smi(rxn_smi, remove_mapping=remove_mapping)
                writer.writerow([rxn_class, i, rxn_smi_canon])
            
    print(f'Finished all phases! Elapsed: {time.time() - start:.2f} secs')
    # very fast, ~60 sec for USPTO-50k

def prep_canon_mt():
    start = time.time()
    # rxn_class = "UNK"
    for phase in ['train', 'valid', 'test']:
        with open(f'rxnebm/data/cleaned_data/50k_clean_rxnsmi_noreagent_{phase}.pickle', 'rb') as handle:
            rxn_smis = pickle.load(handle)

        with open(f'rxnebm/data/cleaned_data/retrosynthesis-{phase}.smi', mode='w') as f:            
            for i, rxn_smi in enumerate(tqdm(rxn_smis, desc=f'Writing rxn_smi in {phase}')):
                rxn_smi_canon, _, _ = canonicalize.canonicalize_rxn_smi(rxn_smi, remove_mapping=False)
                rcts_smi, prod_smi = rxn_smi_canon.split('>>')[0], rxn_smi_canon.split('>>')[-1]
                f.write(prod_smi + ' >> ' + rcts_smi + '\n')
            
    print(f'Finished all phases! Elapsed: {time.time() - start:.2f} secs')
    # very fast, ~60 sec for USPTO-50k

if __name__ == '__main__':
    print(args.output_format)

    if args.output_format == 'gln': # for GLN/RetroXpert
        prep_canon_gln(remove_mapping=False)
    elif args.output_format == 'retroxpert':
        prep_canon_retroxpert(remove_mapping=False)
    elif args.output_format == 'mt': # for MT
        prep_canon_mt()
    else:
        raise ValueError