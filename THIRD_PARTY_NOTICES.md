# 第三方语音组件许可与声明

本文件对应随包的 GPT-SoVITS V2ProPlus 与 IndexTTS 组件。以下许可按各自组件适用，不是本项目原创客户端代码的统一许可证，也不替代其他依赖和素材各自的许可。

## GPT-SoVITS V2ProPlus

- 项目来源：[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)。
- 官方 V2Pro / V2ProPlus 权重来源：[lj1995/GPT-SoVITS · v2Pro](https://huggingface.co/lj1995/GPT-SoVITS/tree/main/v2Pro)，模型仓库标注 MIT。
- 随包项目代码采用 MIT License；原始版权声明为 Copyright (c) 2024 RVC-Boss。
- 完整原文：[GPT-SoVITS-LICENSE.txt](licenses/GPT-SoVITS-LICENSE.txt)。原组件目录中的 `runtime/gpt-sovits/LICENSE` 同时保留。

复制、修改或分发适用的代码及其主要部分时，须保留原版权声明和 MIT 许可全文。嵌套的第三方代码及其他辅助模型仍适用其各自许可，不因上述说明改为 MIT。

## IndexTTS

- 上游项目：[index-tts/index-tts](https://github.com/index-tts/index-tts)。
- 随包组件位于 `runtime/indextts-2.5`，代码和模型附带 bilibili 模型使用许可协议。
- 随包代码协议：[英文原文](licenses/IndexTTS-source-LICENSE.txt)、[中文原文](licenses/IndexTTS-source-LICENSE_ZH.txt)。
- 随包模型协议：[模型目录原文](licenses/IndexTTS-models-LICENSE.txt)。

上述文件均从随包组件原样复制，原目录中的许可文件仍然保留。使用、修改或再分发随包 IndexTTS 及其衍生品时，须遵守对应协议的完整条款；再分发时须保留原始版权声明及协议副本，并确保下游接收者同样遵守协议。本说明不新增原协议之外的授权。

根据随包协议第 4.1(a) 条，对本项目的 IndexTTS 适配声明：

> 该衍生品对原模型所作的任何改动与原模型原始权利人无关，原始权利人对该衍生品不背书、不担保、不承担责任。

分发包含这些组件的源码包或运行包时，应一并保留本文件及对应许可文件。
