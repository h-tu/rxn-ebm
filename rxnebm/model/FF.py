from typing import List, Optional

import torch
import torch.nn as nn

from rxnebm.model import model_utils

Tensor = torch.Tensor

class FeedforwardEBM(nn.Module): 
    """
    Only supports
      sep: takes as input a tuple (reactants_fp, product_fp)

    hidden_sizes : List[int]
        list of hidden layer sizes for the encoder, from layer 0 onwards 
        e.g. [1024, 512, 256] = layer 0 has 1024 neurons, layer 1 has 512 neurons etc.
    output_size : Optional[int] (Default = 1)
        how many outputs the model should give. for binary classification, this is just 1

    TODO: bayesian optimisation
    """

    def __init__(self, args):
        super().__init__()
        self.model_repr = "FeedforwardEBM"

        if args.rxn_type == "hybrid_all": # [rcts_fp, prod_fp, diff_fp]
            self.rctfp_size = args.rctfp_size
            self.prodfp_size = args.prodfp_size
            self.difffp_size = args.difffp_size
        else:
            raise ValueError(f'Not compatible with {args.rxn_type} fingerprints! Only works with hybrid_all')

        self.encoder_rcts = self.build_encoder(
            args.encoder_dropout, args.encoder_activation, args.encoder_hidden_size, self.rctfp_size
        )
        self.encoder_prod = self.build_encoder(
            args.encoder_dropout, args.encoder_activation, args.encoder_hidden_size, self.prodfp_size
        )
        self.encoder_diff = self.build_encoder(
            args.encoder_dropout, args.encoder_activation, args.encoder_hidden_size, self.difffp_size
        )

        if len(args.out_hidden_sizes) > 0:
            self.output_layer = self.build_encoder(
                args.out_dropout, args.out_activation, args.out_hidden_sizes, args.encoder_hidden_size[-1] * 6 + 1,
                output=True
            )
        else:
            self.output_layer = nn.Sequential(
                                    *[
                                    nn.Dropout(args.out_dropout), 
                                    nn.Linear(args.encoder_hidden_size[-1] * 6 + 1, 1) 
                                    ]
                                )

        model_utils.initialize_weights(self)

    def build(self):
      pass 

    def build_encoder(
        self,
        dropout: float,
        activation: str,
        hidden_sizes_encoder: List[int],
        input_dim: int,
        output: bool = False
    ):
        num_layers = len(hidden_sizes_encoder)
        activation = model_utils.get_activation_function(activation)
        ffn = [
                nn.Linear(input_dim, hidden_sizes_encoder[0])
            ]
        for i, layer in enumerate(range(num_layers - 1)):
            ffn.extend(
                [
                    activation,
                    nn.Dropout(dropout),
                    nn.Linear(hidden_sizes_encoder[i], hidden_sizes_encoder[i + 1]),
                ]
                )
        if output:
            ffn.extend(
                [
                    activation,
                    nn.Dropout(dropout),
                    nn.Linear(hidden_sizes_encoder[-1], 1),
                ]
                )
        return nn.Sequential(*ffn)

    def forward(self, batch: Tensor, probs: Optional[Tensor]=None) -> Tensor:
        """
        batch: a N x K x 1 tensor of N training samples
            each sample contains a positive rxn on the first column,
            and K-1 negative rxns on all subsequent columns
        """
        rcts_embedding = self.encoder_rcts(batch[:, :, :self.rctfp_size])                                   # N x K x embedding_dim (hidden_sizes_encoder[-1])
        prod_embedding = self.encoder_prod(batch[:, :, self.rctfp_size:self.rctfp_size+self.prodfp_size])   # N x K x embedding_dim 
        diff_embedding = self.encoder_diff(batch[:, :, self.rctfp_size+self.prodfp_size:])                  # N x K x embedding_dim 

        similarity = nn.CosineSimilarity(dim=-1)(rcts_embedding, prod_embedding).unsqueeze(dim=-1)          # N x K x 1
        
        combined_embedding = torch.cat([rcts_embedding, prod_embedding, diff_embedding,
                                        prod_embedding * rcts_embedding, diff_embedding * rcts_embedding, 
                                        diff_embedding * prod_embedding, similarity], dim=-1) 

        return self.output_layer(combined_embedding).squeeze(dim=-1)                                        # N x K