"""Single-sequence Index GPT decode using shared PyTorch and static CUDA graphs.

Prefill and sampling stay in the original model. Only one-token transformer
forwards are captured; weights are shared and real KV views are returned so an
unsupported input or failed capture can resume the original implementation.
"""
import functools
import sys

import torch
from torch.nn import functional as F
from transformers.modeling_outputs import CausalLMOutputWithCrossAttentions


class NativeIndexDecoder:
    def __init__(self, model, capacity=2048, use_graph=True):
        self.model = model
        self.original = model.forward
        self.capacity = capacity
        self.use_graph = use_graph
        self.graph = None
        self.enabled = True
        self.active = False
        self.disabled_reason = None
        self.replays = 0
        self.prefills = 0
        self.length = 0
        self.buffers_ready = False
        self.validated = False

    def info(self):
        return {'cuda_graph_enabled':self.enabled and self.graph is not None,
                'cuda_graph_replays':self.replays,
                'cuda_graph_prefills':self.prefills,
                'cuda_graph_validated':self.validated,
                'cuda_graph_fallback':self.disabled_reason}

    def _allocate(self, past):
        first = past[0][0]
        batch, heads, _, dim = first.shape
        if batch != 1: raise ValueError('Index graph supports one sequence')
        shape = (len(past),batch,heads,self.capacity,dim)
        self.keys = torch.zeros(shape,device=first.device,dtype=first.dtype)
        self.values = torch.zeros_like(self.keys)
        self.allowed = torch.zeros(self.capacity,device=first.device,dtype=torch.bool)
        self.position = torch.zeros(1,device=first.device,dtype=torch.long)
        self.mel_position = torch.zeros(1,device=first.device,dtype=torch.long)
        self.token = torch.zeros((1,1),device=first.device,dtype=torch.long)
        self.buffers_ready = True

    @torch.no_grad()
    def prefill(self, past, attention_mask):
        if hasattr(past,'to_legacy_cache'): past = past.to_legacy_cache()
        length = past[0][0].shape[-2]
        if length >= self.capacity: return False
        if not self.buffers_ready: self._allocate(past)
        self.keys.zero_(); self.values.zero_(); self.allowed.zero_()
        for layer,(key,value) in enumerate(past):
            self.keys[layer,:,:,:length].copy_(key)
            self.values[layer,:,:,:length].copy_(value)
        self.allowed[:length].copy_(attention_mask[0,:length].bool())
        self.length = length
        self.prefills += 1
        self.active = True
        return True

    def past(self, length=None):
        length = self.length if length is None else length
        return tuple((self.keys[i,:,:,:length],self.values[i,:,:,:length])
                     for i in range(len(self.model.transformer.h)))

    def _decode(self):
        model = self.model
        hidden = model.embeddings(self.token)
        hidden = hidden + model.text_pos_embedding.emb(self.mel_position).unsqueeze(0)
        # Index replaces GPT positional embeddings with FP32 zeros. Preserve
        # the resulting FP32 residual stream under mixed-precision inference.
        hidden = hidden.float()
        self.allowed.index_fill_(0,self.position,True)
        mask = self.allowed.view(1,1,1,-1)
        for i,block in enumerate(model.transformer.h):
            residual = hidden
            normalized = block.ln_1(hidden)
            attn = block.attn
            query,key,value = attn.c_attn(normalized).split(attn.split_size,dim=-1)
            heads = attn.num_heads
            dim = attn.head_dim
            query = query.view(1,1,heads,dim).transpose(1,2)
            key = key.view(1,1,heads,dim).transpose(1,2)
            value = value.view(1,1,heads,dim).transpose(1,2)
            self.keys[i].index_copy_(2,self.position,key)
            self.values[i].index_copy_(2,self.position,value)
            scale = dim**-.5 if attn.scale_attn_weights else 1.0
            if attn.scale_attn_by_inverse_layer_idx: scale /= i+1
            output = F.scaled_dot_product_attention(query,self.keys[i],self.values[i],
                attn_mask=mask,dropout_p=0.0,is_causal=False,scale=scale)
            output = output.transpose(1,2).reshape(1,1,-1)
            hidden = residual + attn.c_proj(output)
            hidden = hidden + block.mlp(block.ln_2(hidden))
        return model.lm_head(model.transformer.ln_f(hidden))

    def _capture(self):
        stream = torch.cuda.Stream(device=self.token.device)
        current = torch.cuda.current_stream(self.token.device)
        stream.wait_stream(current)
        with torch.cuda.stream(stream):
            for _ in range(3): self._decode()
        current.wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        # No compiler, external kernels, duplicated weights or RNG capture.
        with torch.cuda.graph(graph,stream=stream):
            logits = self._decode()
        current.wait_stream(stream)
        self.graph_logits = logits
        self.graph = graph

    @torch.no_grad()
    def decode(self, input_ids, mel_position):
        if self.length >= self.capacity: raise ValueError('Index graph cache exhausted')
        self.token.copy_(input_ids)
        self.position.fill_(self.length)
        self.mel_position.fill_(mel_position)
        if self.use_graph:
            if self.graph is None: self._capture()
            self.graph.replay()
            logits = self.graph_logits
            self.replays += 1
        else:
            logits = self._decode()
        self.length += 1
        return CausalLMOutputWithCrossAttentions(logits=logits,past_key_values=self.past())

    def install(self):
        @functools.wraps(self.original)
        def accelerated(*args,**kwargs):
            ids = kwargs.get('input_ids')
            past = kwargs.get('past_key_values')
            supported = (not args and ids is not None and ids.ndim==2 and ids.shape[0]==1
                and not self.model.training and kwargs.get('return_dict',True) is not False
                and not kwargs.get('output_attentions') and not kwargs.get('output_hidden_states')
                and kwargs.get('token_type_ids') is None and kwargs.get('inputs_embeds') is None
                and kwargs.get('encoder_hidden_states') is None and kwargs.get('head_mask') is None)
            if not self.enabled or not supported or self.disabled_reason is not None:
                self.active = False
                return self.original(*args,**kwargs)
            if past is None or ids.shape[1]!=1:
                self.active = False
                output = self.original(**kwargs)
                if output.past_key_values is not None:
                    try: self.prefill(output.past_key_values,kwargs['attention_mask'])
                    except Exception as exc: self._disable(exc)
                return output
            if not self.active: return self.original(**kwargs)
            mask = kwargs.get('attention_mask')
            if mask is None or mask.shape[-1]!=self.length+1 or self.length>=self.capacity:
                self.active = False
                return self.original(**kwargs)
            try:
                result = self.decode(ids,mask.shape[-1]-self.model.cached_mel_emb.shape[1])
                if not self.validated:
                    expected = self.original(**kwargs)
                    if not torch.allclose(result.logits,expected.logits,rtol=.02,atol=.03):
                        raise RuntimeError('Index decode numerical validation failed')
                    self.validated = True
                return result
            except Exception as exc:
                self._disable(exc)
                return self.original(**kwargs)
        self.model.forward = accelerated
        return self

    def _disable(self, error):
        self.disabled_reason = type(error).__name__
        self.active = False
        print('Index CUDA Graph disabled; original GPU decode retained: '+self.disabled_reason,file=sys.stderr)
