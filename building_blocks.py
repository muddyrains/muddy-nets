import torch.nn.functional as F
import torch
from torch import nn, einsum
from einops import rearrange, repeat, reduce
from positional_and_masking_utils import apply_rotary_pos_emb
import math


def exists(val):
    return val is not None


def max_neg_value(tensor):
    return -torch.finfo(tensor.dtype).max


"""
Feed-forward networks (FFNs), aka multi-layer perceptrons (MLPs), receive each individual token representation and
perform some computation on them. Typically they consist of a linear projection to a larger dimension, are passed
through an activation function (aka non-linearity), and are then linearly projected back down into the input size.

In transformers, FFNs are 1 of the 2 main building blocks (along with attention). Each token representation is fed
into the FFN in a batch. Thus, the weights in the FFN are shared across the sequence dimension (the first token will use
the exact same FFN as a middle token). Crucially, there is no information exchange across the sequence, unlike attention
or convolution operators that can "combine" information from other representations in the sequence. 

Without FFNs, transformers don't work well: https://arxiv.org/abs/2103.03404
"""


class FFN(nn.Module):
    def __init__(self,
                 dim,  # Input and output dimension size
                 ff_mult=4,  # Hidden layer dimension size multiplier
                 dropout=0.0,  # Features to dropout (between 0 and 1)
                 pre_norm_bool=True,  # Apply layer normalization before the FFN
                 post_norm_bool=False,  # Apply layer normalization after the FFN
                 ):
        """
        This is the "vanilla", or standard FFN used in transformer blocks. We use a GELU activation function because
        that is most common, and the exact choice of activation function should not matter that much. Please see
        https://arxiv.org/abs/2102.11972 - page 8.
        """
        super().__init__()

        # Config
        inner_dim = int(dim * ff_mult)
        self.pre_norm_bool = pre_norm_bool
        self.post_norm_bool = post_norm_bool

        # Functions
        if self.pre_norm_bool:
            self.pre_norm = nn.LayerNorm(dim)

        if self.post_norm_bool:
            self.post_norm = nn.LayerNorm(dim)

        self.net = nn.Sequential(
            nn.Linear(dim, inner_dim),  # Project to more features
            nn.GELU(),  # Activation function
            nn.Dropout(dropout),  # Set some features to 0
            nn.Linear(inner_dim, dim),  # Project back down
        )

    def forward(self, x):
        residual = x  # Store input

        if self.pre_norm_bool:
            x = self.pre_norm(x)  # Normalize the representations before the FFN

        x = self.net(x)  # Send through FFN
        x = x + residual  # Add the layer's input to create a residual/skip connection

        if self.post_norm_bool:
            x = self.post_norm(x)  # Normalize the representations after the residual

        return x


class GLUVariantFFN(nn.Module):
    def __init__(self,
                 dim,  # Input and output dimension size (typically it is d_model)
                 ff_mult,  # Hidden layer dimension size multiplier
                 num_projections=2,  # Number of input projections which are multiplied by each other, element-wise
                 num_gelu=1,  # Number of projections to send through a GELU
                 dropout=0.0,  # Features to dropout (between 0 and 1)
                 pre_norm_bool=True,  # Apply layer normalization before the FFN
                 post_norm_bool=False,  # Apply layer normalization after the FFN
                 ):
        """
        Gated Linear Unit (GLU) variants for feedforward networks. See: https://arxiv.org/abs/2002.05202

        Examples:
        GEGLU ---> default config
        Bilinear ---> num_gelu=0, remainder are default
        Trilinear ---> num_projections=3, num_gelu=0, remainder are default

        *WARNING*: Increasing num_projections will increase the parameter count of your model. To match the param count
        of a VanillaFFN with ff_mult=4, use ff_mult=2.667 if num_projections=2, ff_mult=2 if num_projections=3, or
        ff_mult=1.6 if num_projections=4
        """
        super().__init__()

        # Config
        inner_dim = int(ff_mult*dim)
        assert 4 >= num_projections >= 2, "num_projections must be 2, 3, or 4"
        assert num_projections >= num_gelu >= 0, "num_gelu must be >= 0, and <= num_projections"
        assert inner_dim % num_projections == 0, "num_projections must divide evenly into inner_dim"

        self.dim = dim
        self.num_projections = num_projections
        self.num_gelu = num_gelu
        self.pre_norm_bool = pre_norm_bool
        self.post_norm_bool = post_norm_bool

        # Functions
        if self.pre_norm_bool:
            self.pre_norm = nn.LayerNorm(dim)

        if self.post_norm_bool:
            self.post_norm = nn.LayerNorm(dim)

        self.proj_up = nn.Linear(dim, inner_dim * num_projections)
        self.dropout = nn.Dropout(dropout)
        self.proj_down = nn.Linear(inner_dim, dim)

    def forward(self, x):
        residual = x  # Store input

        if self.pre_norm_bool:
            x = self.pre_norm(x)  # Normalize the representations before the FFN

        # Linearly project up to inner_dim * num_projections features, then split into chunks of equal shape
        x = self.proj_up(x).chunk(self.num_projections, dim=-1)

        if self.num_gelu > 0:
            # Loop through every chunk, if the chunk index is less than self.num_gelu, then apply a GELU
            # This will result in GELU(s) being applied to self.num_gelu chunks
            x = [F.gelu(chunk) if _idx < self.num_gelu else chunk for _idx, chunk in enumerate(x)]

        # Multiply the chunks by each other, element-wise
        if self.num_projections == 2:
            x = x[0] * x[1]
        elif self.num_projections == 3:
            x = x[0] * x[1] * x[2]
        elif self.num_projections == 4:
            x = x[0] * x[1] * x[2] * x[3]
        else:
            raise "self.num_projections out of range, inside of forward pass"

        x = self.dropout(x)
        x = self.proj_down(x)  # Project back down
        x = x + residual  # Add the layer's input to create a residual/skip connection

        if self.post_norm_bool:
            x = self.post_norm(x)  # Normalize the representations after the residual

        return x


"""
Attention is the second main building block used in transformers. Simply, attention allows for inputs to "see", or "take
into account" other inputs in the sequence. (insert link to explainer blogs)

The attention mechanism is expensive because every element in the sequence must be compared with every other element.
This leads to the computational cost growing with the sequence length *squared*. There have been dozens of attention 
variants that lower the computational cost of attention, but thus far, none provide similar performance. 

According to lucidrains, the routing transformer and pooling transformers are the most promising. But unless you are
working with very long sequences (i.e. in the thousands of tokens) - stick with the vanilla attention mechanism.
"""


class Attention(nn.Module):
    def __init__(self,
                 dim,  # Input and output dimension size (typically it is d_model)
                 attn_dim,  # Dimension size of attention (typically it is equal to dim)
                 num_heads,  # Number of attention heads
                 previous_attention_bool=False,  # Whether or not to re-use the last attention map
                 pre_norm_bool=True,  # Apply layer normalization before attention
                 post_norm_bool=False,  # Apply layer normalization after attention
                 ):
        """
        Standard attention function, with a few features. Lazy attention (set previous_attention_bool=True) allows us
        to skip calculating a new attention map, and re-use the last attention map: https://arxiv.org/abs/2102.12702 .
        When not using lazy attention, we can use residual attention (https://arxiv.org/abs/2012.11747) by giving this
        module previous_attn_dots, which are the dots from the last attention layer.
        """
        super().__init__()

        # Config
        dim_head = int(attn_dim / num_heads)
        assert attn_dim % num_heads == 0, "The attention dimension size (attn_dim) must divide evenly into num_heads"

        self.scale = dim_head ** -0.5
        self.num_heads = num_heads
        self.previous_attention_bool = previous_attention_bool
        self.pre_norm_bool = pre_norm_bool
        self.post_norm_bool = post_norm_bool

        # Functions
        if self.previous_attention_bool:
            # If we use the attention pattern from the last attention layer, we don't need queries and keys
            self.to_v = nn.Linear(dim, attn_dim, bias=False)

        else:
            # Standard attention layer that will calculate the attention pattern from queries and keys
            self.to_q = nn.Linear(dim, attn_dim, bias=False)
            self.to_k = nn.Linear(dim, attn_dim, bias=False)
            self.to_v = nn.Linear(dim, attn_dim, bias=False)

        if self.pre_norm_bool:
            self.pre_norm = nn.LayerNorm(dim)

        if self.post_norm_bool:
            self.post_norm = nn.LayerNorm(dim)

        self.attn_fn = F.softmax
        self.to_out = nn.Linear(attn_dim, dim)

    def forward(self,
                x,  # The source of queries and keys/values if there is no context
                context=None,  # The source of keys/values, aka memory
                mask=None,
                positional_bias_fn=None,  # Positional bias which is added to dots
                previous_attn_map=None,  # The attention map (after softmax) from the last attention calculation
                previous_attn_dots=None,  # The attention dots (before softmax) from the last attention calculation
                rotary_pos_emb=None,  # RoPE embeddings
                ):

        residual = x  # Store input

        if self.pre_norm_bool:
            x = self.pre_norm(x)  # Normalize the representations before attention

        if exists(previous_attn_map) and self.previous_attention_bool:  # Re-use the last attention map
            if exists(context):
                v_input = context  # Used for cross-attention
            else:
                v_input = x  # Used for self-attention

            v = self.to_v(v_input)  # Create values via a linear projection
            attn_map = previous_attn_map  # Set the current attention map to the last map (includes last mask)
            dots = None  # We did not calculate any attention dots

        elif not self.use_previous_attention:  # Calculate attention map from queries and keys
            if exists(context):
                # For cross-attention, the layer's input are queries, and the keys/values come from the context
                q_input = x
                k_input = context
                v_input = context
            else:
                # For self-attention, the qkv inputs all start from x (this layer's input)
                q_input = x
                k_input = x
                v_input = x

            q = self.to_q(q_input)  # create queries via a linear projection
            k = self.to_k(k_input)  # create keys via a linear projection
            v = self.to_v(v_input)  # create values via a linear projection

            # For q, k, and v, rearrange the features into heads (hence the name multi-headed attention)
            q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.num_heads), (q, k, v))

            if exists(rotary_pos_emb):
                # Implementation is taken 100% from lucidrains
                # We want to rotate the q, k, and v to incorporate positional info. If we only want to rotate a portion
                # of the features, then we must slice the tensors, rotate the slice, then piece them back together with
                # the un-rotated portion of the tensor

                l = rotary_pos_emb.shape[-1]  # The number of features we wish to slice then rotate
                (ql, qr), (kl, kr), (vl, vr) = map(lambda t: (t[..., :l], t[..., l:]), (q, k, v))  # Slice qkv
                ql, kl, vl = map(lambda t: apply_rotary_pos_emb(t, rotary_pos_emb), (ql, kl, vl))  # Rotate the slices
                q, k, v = map(lambda t: torch.cat(t, dim=-1), ((ql, qr), (kl, kr), (vl, vr)))  # Piece back together

            # Perform a dot product between the queries and keys, along the feature dimension. The resultant tensor,
            # dots, is a measure of similarity between the features
            # TBD: Explain einsum notation, a bit
            dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

            if exists(previous_attn_dots):
                # Add attention dots residual connection
                dots = dots + previous_attn_dots

            if exists(positional_bias_fn):
                dots = positional_bias_fn(dots)  # Apply a positional bias to the attention map

            # if exists(mask):
            #     dots = dots + mask  # Add negative infinity to masked positions, and 0 elsewhere
            # Use Lucid's masking implementation for now
            # Set to causal for testing

            mask_value = max_neg_value(dots)
            i, j = dots.shape[-2:]
            r = torch.arange(i)
            mask = rearrange(r, 'i -> () () i ()') < rearrange(r, 'j -> () () () j')

            mask = F.pad(mask, (j - i, 0), value=False)
            dots.masked_fill_(mask, mask_value)

            attn_map = self.attn_fn(dots, dim=-1)  # Take the softmax over the length of the sequence (keys/values)

        else:
            raise "If self.previous_attention_bool is True, previous_attn_map needs to be given"

        x = einsum('b h i j, b h j d -> b h i d', attn_map, v)  # Weighted sum of value heads based on attn_map
        x = rearrange(x, 'b h n d -> b n (h d)')  # Merge the heads back together so we have the same number of
        # features as our input

        x = self.to_out(x)  # Send through a final linear projection
        x = x + residual  # Add the layer's input to create a residual/skip connection

        if self.post_norm_bool:
            x = self.post_norm(x)  # Normalize the representations after attention

        return x, attn_map, dots  # Return the output, attention map, and the dots (in case we need them later)

