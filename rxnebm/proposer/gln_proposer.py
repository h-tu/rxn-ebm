from gln.test.model_inference import RetroGLN
from proposer_base import Proposer
from typing import Dict, List


class GLNProposer(Proposer):
    """GLN proposer, wrapping around GLN.gln.test.model_inference.RetroGLN"""

    def __init__(self, gln_config: Dict) -> None:
        super().__init__()
        self.model = self.build_model(gln_config)

    @staticmethod
    def build_model(gln_config: Dict) -> RetroGLN:
        model = RetroGLN(dropbox=gln_config["dropbox"],
                         model_dump=gln_config["model_path"])

        return model

    def propose(self, input_smiles: List[str],
                rxn_types: List[str],
                topk: int = 1, **kwargs) -> List[Dict[str, List]]:

        results = []
        for smi, rxn_type in zip(input_smiles, rxn_types):
            result = self.model.run(raw_prod=smi,
                                    beam_size=50,
                                    topk=topk,
                                    rxn_type=rxn_type)
            results.append(result)

        return results
