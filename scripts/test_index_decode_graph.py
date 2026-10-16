"""Compare the real Index GPT forward with static-cache decode; no weights/audio."""
from pathlib import Path
import os
import sys
import unittest
import json
import tempfile
from unittest.mock import patch
import torch
from transformers import GPT2Config, GPT2Model
from index_decode_graph import NativeIndexDecoder

ROOT=Path(__file__).resolve().parents[1]
sys.path.extend([str(ROOT/'runtime/indextts-2.5/source'),str(ROOT/'runtime/indextts-2.5/extras')])
from indextts.gpt.model_v2 import GPT2InferenceModel,LearnedPositionEmbeddings,null_position_embeddings
import functools


def fixture(device='cpu'):
    torch.manual_seed(31)
    config=GPT2Config(vocab_size=19,n_positions=80,n_ctx=80,n_embd=32,n_layer=2,n_head=4,
        resid_pdrop=0,embd_pdrop=0,attn_pdrop=0,bos_token_id=3,eos_token_id=18,pad_token_id=18)
    config._attn_implementation='sdpa'
    transformer=GPT2Model(config)
    del transformer.wpe
    transformer.wpe=functools.partial(null_position_embeddings,dim=32)
    model=GPT2InferenceModel(config,transformer,LearnedPositionEmbeddings(80,32),
        torch.nn.Embedding(19,32),torch.nn.LayerNorm(32),torch.nn.Linear(32,19),kv_cache=True).eval().to(device)
    if device=='cuda': model=model.bfloat16()
    model.store_mel_emb(torch.randn(1,4,32,device=device,dtype=next(model.parameters()).dtype))
    return model


class DecodeTests(unittest.TestCase):
    def test_synthesis_options_validate_and_keep_gpt_configuration_separate(self):
        from index_voice_backend import synthesis_options
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'options.json'
            self.assertEqual(synthesis_options(path,{}),{'cuda_graph':True,'diffusion_steps':20})
            path.write_text(json.dumps({'cuda_graph':False,'diffusion_steps':25}))
            self.assertEqual(synthesis_options(path,{}),{'cuda_graph':False,'diffusion_steps':25})
            self.assertEqual(synthesis_options(path,{'RONOVA_INDEX_CUDA_GRAPH':'1','RONOVA_INDEX_DIFFUSION_STEPS':'15'}),
                             {'cuda_graph':True,'diffusion_steps':15})
            for value in (True,0,12,'20'):
                path.write_text(json.dumps({'diffusion_steps':value}))
                with self.assertRaises(ValueError): synthesis_options(path,{})

    @torch.no_grad()
    def check_logits(self,device='cpu',use_graph=False):
        model=fixture(device); original=model.forward
        decoder=NativeIndexDecoder(model,capacity=32,use_graph=use_graph)
        ids=torch.tensor([[1,1,1,1,3]],device=device)
        mask=torch.tensor([[0,1,1,1,1]],device=device)
        expected=original(input_ids=ids,attention_mask=mask,use_cache=True,return_dict=True)
        self.assertTrue(decoder.prefill(expected.past_key_values,mask))
        past=expected.past_key_values
        for token in (4,7,2,8,1):
            ids=torch.tensor([[token]],device=device);mask=torch.cat((mask,mask.new_ones(1,1)),dim=-1)
            expected=original(input_ids=ids,past_key_values=past,attention_mask=mask,use_cache=True,return_dict=True)
            actual=decoder.decode(ids,mask.shape[-1]-model.cached_mel_emb.shape[1])
            torch.testing.assert_close(actual.logits,expected.logits,rtol=.02 if device=='cuda' else 1e-4,
                                       atol=.02 if device=='cuda' else 1e-5)
            for (ak,av),(ek,ev) in zip(actual.past_key_values,expected.past_key_values):
                torch.testing.assert_close(ak,ek,rtol=.02 if device=='cuda' else 1e-4,atol=.02 if device=='cuda' else 1e-5)
                torch.testing.assert_close(av,ev,rtol=.02 if device=='cuda' else 1e-4,atol=.02 if device=='cuda' else 1e-5)
            past=expected.past_key_values
        self.assertEqual(decoder.length,10)
        if use_graph: self.assertEqual(decoder.replays,5)
        # A new utterance clears previous masks, keys and lengths.
        newmask=torch.ones((1,5),device=device,dtype=torch.long)
        prefix=original(input_ids=torch.ones((1,5),device=device,dtype=torch.long),attention_mask=newmask,use_cache=True,return_dict=True)
        decoder.prefill(prefix.past_key_values,newmask)
        self.assertEqual(decoder.length,5);self.assertEqual(decoder.allowed.sum().item(),5)

    def test_static_decode_logits_cache_and_padding_match_original(self): self.check_logits()

    @torch.no_grad()
    def test_capture_failure_resumes_original_forward_with_real_cache(self):
        model=fixture();decoder=NativeIndexDecoder(model,capacity=32,use_graph=True).install()
        prefix=model(input_ids=torch.ones((1,5),dtype=torch.long),attention_mask=torch.ones(1,5),use_cache=True,return_dict=True)
        kwargs=dict(input_ids=torch.tensor([[2]]),past_key_values=prefix.past_key_values,
                    attention_mask=torch.ones(1,6),use_cache=True,return_dict=True)
        with patch.object(decoder,'_capture',side_effect=RuntimeError('fixture capture failed')):
            actual=model(**kwargs)
        expected=decoder.original(**kwargs)
        torch.testing.assert_close(actual.logits,expected.logits)
        self.assertEqual(actual.past_key_values[0][0].shape[-2],6)
        self.assertEqual(decoder.disabled_reason,'RuntimeError')

    @torch.no_grad()
    def test_capacity_exhaustion_keeps_valid_original_cache(self):
        model=fixture();decoder=NativeIndexDecoder(model,capacity=6,use_graph=False).install()
        result=model(input_ids=torch.ones((1,5),dtype=torch.long),attention_mask=torch.ones(1,5),use_cache=True,return_dict=True)
        for length in (6,7,8):
            result=model(input_ids=torch.tensor([[2]]),past_key_values=result.past_key_values,
                         attention_mask=torch.ones(1,length),use_cache=True,return_dict=True)
            self.assertEqual(result.past_key_values[0][0].shape[-2],length)

    @unittest.skipUnless(os.environ.get('RONOVA_TEST_CUDA_GRAPH')=='1','explicit GPU check only')
    def test_cuda_graph_replay_matches_original_bf16(self):
        with torch.amp.autocast('cuda',dtype=torch.bfloat16):
            self.check_logits('cuda',True)

if __name__=='__main__':unittest.main()
