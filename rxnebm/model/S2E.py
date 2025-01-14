import logging
import torch
import torch.nn as nn
from onmt.encoders import TransformerEncoder
from onmt.modules.embeddings import Embeddings as ONMTEmbeddings
from rxnebm.model import model_utils
from typing import Dict, Optional

Tensor = torch.tensor

def sequence_mask(lengths, maxlen: int):
    """https://discuss.pytorch.org/t/pytorch-equivalent-for-tf-sequence-mask/39036"""
    if maxlen is None:
        maxlen = lengths.max()
    mask = ~(torch.ones((len(lengths), maxlen), device=lengths.device).cumsum(dim=1).t() > lengths).t()
    mask = mask.transpose(0, 1)
    mask.long()
    return mask


class TransformerEBM(nn.Module):
    def __init__(self, args,
                 vocab: Dict[str, int]):
        super().__init__()
        self.model_repr = "TransformerEBM"
        self.args = args
        self.vocab = vocab
        self.vocab_size = len(vocab)
        assert len(args.encoder_hidden_size) == 1
        self.hidden_size = args.encoder_hidden_size[0]
        self.pooling_method = args.s2e_pool_type

        self.encoder_embeddings = ONMTEmbeddings(
            word_vec_size=args.encoder_embed_size,
            word_vocab_size=self.vocab_size,
            word_padding_idx=self.vocab["_PAD"],
            position_encoding=True
        )

        self.encoder = TransformerEncoder(
            num_layers=args.encoder_depth,
            d_model=self.hidden_size,
            heads=args.encoder_num_heads,
            d_ff=args.encoder_filter_size,
            dropout=args.encoder_dropout,
            attention_dropout=args.attention_dropout,
            embeddings=self.encoder_embeddings,
            max_relative_positions=0
        )

        if self.args.prob_file_prefix:
            self.output = nn.Linear(self.hidden_size + 1, 1)
        else:
            self.output = nn.Linear(self.hidden_size, 1)
        logging.info("Initializing weights for transformer")
        model_utils.initialize_weights(self, transformer=True)

    def forward(self, batch):
        """
        batch: a N x K x 1 tensor of N training samples
            each sample contains a positive rxn on the first column,
            and K-1 negative rxns on all subsequent columns
        """
        batch, phase = batch
        batch_token_ids, batch_lens, batch_size = batch             # [N * K, t], [N * K]
        enc_in = batch_token_ids.transpose(0, 1).unsqueeze(-1)      # [N * K, t] => [t, N * K, 1]
        lengths = torch.tensor([self.args.max_seq_len] * batch_lens.shape[0],
                               dtype=torch.long,
                               device=batch_lens.device)

        _, encodings, _ = self.encoder(src=enc_in,
                                       lengths=lengths)             # [t, N * K, h]
        seq_masks = sequence_mask(                                  # [N * K] => [t, N * K]
            batch_lens, maxlen=self.args.max_seq_len)
        seq_masks = seq_masks.unsqueeze(-1)                         # [t, N * K] => [t, N * K, 1]

        encodings = encodings * seq_masks                           # mask out padding
        K = self.args.minibatch_size if phase == 'train' else self.args.minibatch_eval
        encodings = torch.reshape(encodings,                        # [t, N * K, h] => [t, N, K, h]
                                [self.args.max_seq_len,
                                -1, # batch_size
                                K,
                                self.hidden_size])
        batch_lens = torch.reshape(batch_lens,                      # [N * K] => [N, K, 1]
                                [-1, #batch_size,
                                K, 
                                1])

        if self.pooling_method == "CLS":                            # [t, N, K, h] => [N, K, h]
            pooled_encoding = encodings[0, :, :, :]
        elif self.pooling_method == "mean":
            pooled_encoding = torch.sum(encodings, dim=0, keepdim=False)
            pooled_encoding = pooled_encoding / batch_lens
        elif self.pooling_method == 'sum':
            pooled_encoding = torch.sum(encodings, dim=0, keepdim=False)
        else:
            raise ValueError(f"Unsupported pooling method: {self.pooling_method}")
        
        energies = self.output(pooled_encoding)                 # [N, K, h] => [N, K, 1]
        return energies.squeeze(dim=-1)                             # [N, K]
