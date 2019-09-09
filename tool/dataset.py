"""
Author: Lukas Gosch
Date: 5.9.2019
Description:
    Functions to preprocess proteins for classification.
"""
import os
from itertools import islice
from collections import namedtuple
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import IterableDataset
from torch.utils.data.dataloader import default_collate
from Bio import SeqIO
from Bio.Alphabet.IUPAC import ExtendedIUPACProtein

collated_sequences = namedtuple('collated_sequences', ['sequences', 'ids'])

def collate_sequences(batch, zero_padding=True):
    """ Collate and zero_pad encoded sequence. """

    # Check if an individual sample or a batch was given
    if not isinstance(batch, list):
        batch = [batch]
        
    # Find the longest sequence, in order to zero pad the others
    max_len, n_features = 0, 1  # batch.query_encoded.shape
    n_data = 0
    for seq in batch:
        query = seq.encoded
        n_data += 1
        sequence_len = len(query)
        if sequence_len > max_len:
            max_len = sequence_len

    # Collate the sequences
    if zero_padding:
        sequences = np.zeros((n_data, max_len,), dtype=np.int)
        for i, seq in enumerate(batch):
            query = np.array(seq.encoded)
            start = 0
            end = len(query)
            # Zero pad
            sequences[i, start:end] = query[:].T
        # Convert NumPy array to PyToch Tensor
        sequences = default_collate(sequences)
    else:  
        # no zero-padding, must use minibatches of size 1 downstream!
        raise NotImplementedError('Batching requires zero padding!')
       
    # Collate the ids
    ids = [seq.id for seq in batch]

    return collated_sequences(sequences=sequences,
                              ids=ids)


class AminoAcidWordEmbedding(nn.Module):
    """ PyTorch nn.Embedding where each amino acid is considered one word.

    Parameters
    ----------
    embedding_dim: int
        Embedding dimensionality.
    """
    def __init__(self, embedding_dim=10):
        super(AminoAcidWordEmbedding, self).__init__()
        # Get protein sequence vocabulary
        self.vocab = self.gen_amino_acid_vocab()
        # Create embedding (initialized randomly)
        embeds = nn.Embedding(len(self.vocab) // 2 + 1, embedding_dim)
        self.embedding = embeds

    @staticmethod
    def gen_amino_acid_vocab(alphabet=None):
        """ Create vocabulary for protein sequences. """
        if alphabet is None:
            # Use all 26 letters
            alphabet = ExtendedIUPACProtein()

        # In case of ExtendendIUPACProtein: Map 'ACDEFGHIKLMNPQRSTVWYBXZJUO' 
        # to [1, 26] so that zero padding does not interfere.
        aminoacid_to_ix = {}
        for i, aa in enumerate(alphabet.letters):
            # Map both upper case and lower case to the same embedding
            for key in [aa.upper(), aa.lower()]:
                aminoacid_to_ix[key] = i + 1
        vocab = aminoacid_to_ix
        return vocab

    def forward(self, sequence):
        x = self.embedding(sequence)
        return x

def consume(iterator, n=None):
    """ Advance the iterator n-steps ahead. If n is None, consume entirely.

        Function from Itertools Recipes in official Python 3.7.4. docs.
    """
    # Use functions that consume iterators at C speed.
    if n is None:
        # feed the entire iterator into a zero-length deque
        collections.deque(iterator, maxlen=0)
    else:
        # advance to the empty slice starting at position n
        next(islice(iterator, n, n), None)


class ProteinIterator():
    """ Iterator allowing for multiprocess dataloading of a sequence file. 
        
        MPProteinIterator is a wrapper for the iterator returned by 
        Biopythons Bio.SeqIO class when parsing a sequence file. It 
        specifies custom __next__() method to support multiprocess data 
        loading. It does so by each worker skipping num_worker - 1 data 
        samples for each call to __next__(). Furthermore, each worker skips
        worker_id data samples in the initialization.

        It also makes sure that a unique ID is set in each SeqRecord 
        optained from the data-iterator. The id attribute in each SeqRecord 
        is prefixed by an index i which directly corresponds to the i-th 
        sequence in the sequence file. 

        Parameters
        ----------
        iterator
            Iterator over sequence file returned by Biopythons
            Bio.SeqIO.parse() function.
        num_workers : int
            Number of workers set in DataLoader
        worker_id : int
            ID of worker this iterator belongs to
    """
    def __init__(self, iterator, num_workers=1, worker_id=0):
        self.iterator = iterator
        # Start position
        self.start = worker_id
        self.pos = None
        # Number of sequences to skip for each next() call.
        self.step = num_workers - 1
        # Make Dataset return namedtuple
        self.sequence = namedtuple('sequence', ['id', 'string', 'encoded'])

    def __iter__(self):
        return self

    def __next__(self):
        """ Return correctly prefixed sequence object. 
            
            Returns element at current + step + 1 position or start 
            position. Fruthermore prefixes element with unique sequential
            ID.
        """
        # Check if iterator has been positioned correctly.
        if self.pos is not None:
            consume(self.iterator, n=self.step)
            self.pos += self.step + 1
        else:
            consume(self.iterator, n=self.start)
            self.pos = self.start + 1
        next_seq = next(self.iterator)
        alphabet = next_seq.seq.alphabet
        vocab = AminoAcidWordEmbedding.gen_amino_acid_vocab(alphabet)
        # Generate sequence object from SeqRecord
        sequence = self.sequence(id = f'{self.pos}_{next_seq.id}',
                                 string = str(next_seq.seq),
                                 encoded = [vocab[c] for c in next_seq.seq])
        return sequence

class ProteinDataset(IterableDataset):
    """ Protein dataset holding the proteins to classify. 
    
    Parameters
    ----------
    file : str
        Path to file storing the protein sequences.
    f_format : str
        File format in which to expect the protein sequences. Must
        be supported by Biopythons Bio.SeqIO class.
    max_length : int
        If only proteins up to a certain length should be loaded.
        Defaults to None, meaning no length constraint
    zero_padding : bool
        Default behaviour is to zero pad all sequences up to
        the length of the longest one by appending zeros at the end. 
        If max_length is set, zero pads all sequences up to 
        max_length. False deactivates any zero padding.
    """

    def __init__(self, file, f_format='fasta', max_length=None,
                 zero_padding = True):
        """ Initialize iterator over sequences in file."""
        if os.path.isfile(file):
            self.iter = SeqIO.parse(file, format = f_format, 
                                        alphabet = ExtendedIUPACProtein())
        else:
            raise ValueError('Given file does not exist or is not a file.')
        
    def __iter__(self):
        """ Return iterator over sequences in file. """
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            return ProteinIterator(self.iter)
        else:
            return ProteinIterator(self.iter, worker_info.num_workers,
                                   worker_info.id)
            #raise NotImplementedError('Multiprocess-dataloading not supported.')


# class ProteinIterator():
#     """ Iterator over sequence file used by ProteinDataset.

#         ProteinIterator wraps the iterator returned by the Bio.SeqIO.parse()
#         function. It makes sure that a unique ID is set in each SeqRecord 
#         optained from the data-iterator. The id attribute in each SeqRecord 
#         is prefixed by an index i which directly corresponds to the i-th 
#         sequence in the sequence file. 

#         Parameters
#         ----------
#         iterator
#             Iterator over sequence file returned by Biopythons
#             Bio.SeqIO.parse() function.
#     """
#     def __init__(self, iterator):
#         self.iterator = iterator
#         self.position = 0

#     def __iter__(self):
#         return self

#     def __next__(self):
#         """ Return next SeqRecrod in iterator and prefix id. """
#         next_seq = next(self.iterator)
#         self.position += 1
#         next_seq.id = f'{self.position}_{next_seq.id}'
#         return next_seq 