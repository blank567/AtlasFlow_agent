"""Read-only dataset compiler. Prints JSON; apply reviewed output through apply_patch.

No ranking, Run, model answer, or network request is synthesized. Anchor verification
only establishes raw-source location, not correctness of generated answers.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / "data/knowledge/corpus/llm-engineering-v1"

# query, tags, (source, literal raw anchor) pairs. Quotes are short locators,
# not replacements for original documents or full model-generated reference answers.
SPECS = [
    (
        "Transformer为什么不依赖循环网络和卷积？",
        ["zh", "fundamentals"],
        [("attention-is-all-you-need", "dispensing with recurrence and convolutions entirely")],
    ),
    (
        "Which two components do sequence transduction models commonly include?",
        ["en", "fundamentals"],
        [("attention-is-all-you-need", "an encoder and a decoder")],
    ),
    (
        "Chinchilla建议模型规模翻倍时训练Token如何变化？",
        ["zh", "training"],
        [
            (
                "chinchilla",
                "for every doubling of model size the number of training tokens should also be doubled",
            )
        ],
    ),
    (
        "How should model size and training tokens scale under a compute-optimal budget?",
        ["en", "training"],
        [
            (
                "chinchilla",
                "the model size and the number of training tokens should be scaled equally",
            )
        ],
    ),
    (
        "InstructGPT的监督学习数据来自什么？",
        ["zh", "alignment"],
        [("instructgpt", "labeler demonstrations of the desired model behavior")],
    ),
    (
        "RLHF在InstructGPT中收集什么类型的模型输出反馈？",
        ["mixed", "alignment"],
        [("instructgpt", "rankings of model outputs")],
    ),
    ("LoRA引入哪种可训练矩阵？", ["zh", "training"], [("lora", "rank decomposition matrices")]),
    (
        "What trainable-parameter reduction does the LoRA abstract report for GPT-3?",
        ["en", "training"],
        [("lora", "10,000 times")],
    ),
    (
        "FlashAttention主要减少GPU哪两层存储之间的读写？",
        ["zh", "inference"],
        [("flashattention", "GPU high bandwidth memory (HBM) and GPU on-chip SRAM")],
    ),
    (
        "Is FlashAttention described as exact or approximate attention?",
        ["en", "inference"],
        [("flashattention", "an IO-aware exact attention algorithm")],
    ),
    (
        "vLLM的PagedAttention对KVCache内存浪费有什么改善？",
        ["mixed", "inference"],
        [("pagedattention", "near-zero waste in KV cache memory")],
    ),
    (
        "What operating-system memory idea inspires PagedAttention?",
        ["en", "inference"],
        [("pagedattention", "classical virtual memory")],
    ),
    (
        "DeepSeek-V3总参数和每Token激活参数分别是多少？",
        ["zh", "models"],
        [("deepseek-v3-report", "671B total parameters with 37B activated for each token")],
    ),
    (
        "DeepSeek-V3采用的MLA全称是什么？",
        ["mixed", "models"],
        [("deepseek-v3-report", "Multi-head Latent Attention (MLA)")],
    ),
    (
        "Qwen2.5预训练语料扩展到多少Token？",
        ["zh", "models"],
        [("qwen2.5-report", "18 trillion tokens")],
    ),
    (
        "Which offline and online reinforcement-learning methods does Qwen2.5 name?",
        ["en", "models"],
        [("qwen2.5-report", "offline learning DPO and online learning GRPO")],
    ),
    (
        "ReAct中的推理轨迹如何帮助Agent管理行动计划？",
        ["zh", "agents"],
        [("react", "reasoning traces help the model induce, track, and update action plans")],
    ),
    (
        "How do actions in ReAct help obtain external information?",
        ["en", "agents"],
        [("react", "gather additional information from external sources")],
    ),
    (
        "RAG综述指出大模型会遇到哪些知识问题？",
        ["zh", "rag"],
        [("rag-survey", "hallucination, outdated knowledge")],
    ),
    (
        "What knowledge-update benefit is stated for RAG?",
        ["en", "rag"],
        [("rag-survey", "continuous knowledge updates")],
    ),
    (
        "2026年的LLM综述主要研究什么模型类别？",
        ["zh", "fundamentals"],
        [("llm-survey-2026", "large language models (LLMs)")],
    ),
    (
        "多Agent综述提到LLM多Agent在哪两类场景取得进展？",
        ["zh", "agents"],
        [("multi-agent-survey", "complex problem-solving and world simulation")],
    ),
    (
        "Transformers默认使用哪种Cache？",
        ["zh", "inference"],
        [("transformers-kv-cache", "is the default cache class for all models")],
    ),
    (
        "Which cache strategy supports torch.compile according to the cache comparison?",
        ["en", "inference"],
        [("transformers-kv-cache", "Static Cache")],
    ),
    (
        "KVCache为什么能减少自回归推理的重复计算？",
        ["mixed", "inference"],
        [
            (
                "transformers-cache-explanation",
                "storing kv pairs derived from the attention layers of previously processed tokens",
            )
        ],
    ),
    (
        "Why should caching not be enabled during training?",
        ["en", "inference"],
        [
            (
                "transformers-cache-explanation",
                "It may cause unexpected errors if it's enabled during training.",
            )
        ],
    ),
    (
        "Continuous Batching何时重新调度batch？",
        ["mixed", "inference"],
        [
            (
                "transformers-continuous-batching",
                "dynamically rescheduling the batch at every generation step",
            )
        ],
    ),
    (
        "Does generate_batch return before every request has completed?",
        ["en", "inference"],
        [
            (
                "transformers-continuous-batching",
                "handles scheduling internally and blocks until all requests are complete.",
            )
        ],
    ),
    (
        "Transformers优化概述提到哪些降低内存流量的技术？",
        ["zh", "inference"],
        [
            (
                "transformers-optimization",
                "alternative attention mechanisms for reduced memory traffic",
            )
        ],
    ),
    (
        "LLMWiki如何维护持久化知识而不是每次查询重新推导？",
        ["zh", "wiki-interface"],
        [("microsoft-llmwiki-readme", "incrementally builds and maintains a persistent wiki")],
    ),
    (
        "LLMWiki架构中的wiki层承担什么职责？",
        ["zh", "wiki-interface"],
        [
            (
                "microsoft-llmwiki-architecture",
                "LLM-generated markdown files — the compiled knowledge base.",
            )
        ],
    ),
    (
        "对比KVCache和FlashAttention，分别优化哪种计算或访存？",
        ["zh", "cross_document"],
        [
            (
                "transformers-cache-explanation",
                "storing kv pairs derived from the attention layers of previously processed tokens",
            ),
            ("flashattention", "GPU high bandwidth memory (HBM) and GPU on-chip SRAM"),
        ],
    ),
    (
        "Compare LoRA adaptation and Chinchilla compute-optimal training objectives.",
        ["en", "cross_document"],
        [
            ("lora", "rank decomposition matrices"),
            (
                "chinchilla",
                "the model size and the number of training tokens should be scaled equally",
            ),
        ],
    ),
    (
        "ReAct的行动机制和多Agent协作分别关注什么？",
        ["mixed", "cross_document"],
        [
            ("react", "gather additional information from external sources"),
            ("multi-agent-survey", "complex problem-solving and world simulation"),
        ],
    ),
]
UNANSWERABLE = [
    "今天美元兑人民币的实时汇率是多少？",
    "What is the current weather in Huangshan?",
    "请提供我公司的私有APIKey。",
    "2026年10月14日北京大学东门到颐和园的实时交通耗时是多少？",
    "What was the private evaluation score of an unpublished model yesterday?",
    "这套论文中的作者明天几点起床？",
]


def compile_datasets():
    logging.disable(logging.CRITICAL)
    ledger = json.loads((FOLDER / "import-ledger.json").read_text(encoding="utf-8"))
    hashes = {row["source_id"]: row["sha256"] for row in ledger["documents"]}
    pages = {}
    source_paths = {}
    for source_id, digest in hashes.items():
        path = next(
            (
                FOLDER / (source_id + ext)
                for ext in (".pdf", ".md")
                if (FOLDER / (source_id + ext)).exists()
            ),
            None,
        )
        if path is None:
            raise ValueError(f"source missing: {source_id}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"source hash mismatch: {source_id}")
        source_paths[source_id] = path.relative_to(ROOT).as_posix()
        pages[source_id] = (
            [" ".join(page.extract_text().split()) for page in PdfReader(path).pages]
            if path.suffix == ".pdf"
            else [" ".join(path.read_text(encoding="utf-8").split())]
        )
    cases = []
    for index, (query, tags, anchors) in enumerate(SPECS, 1):
        gold = []
        for source_id, quote in anchors:
            matches = [page + 1 for page, text in enumerate(pages[source_id]) if quote in text]
            if not matches:
                raise ValueError(f"unverified literal anchor: {source_id}: {quote}")
            gold.append(
                {
                    "source_id": source_id,
                    "content_hash": hashes[source_id],
                    "source_path": source_paths[source_id],
                    "anchor": quote,
                    "page_number": matches[0] if source_paths[source_id].endswith(".pdf") else None,
                }
            )
        cases.append(
            {
                "case_id": f"R{index:02}",
                "kind": "rag",
                "query": query,
                "tags": tags,
                "gold_status": "verified",
                "expected_sources": gold,
            }
        )
    for index, query in enumerate(UNANSWERABLE, 35):
        cases.append(
            {
                "case_id": f"R{index:02}",
                "kind": "rag",
                "query": query,
                "tags": ["no_answer"],
                "answerable": False,
                "gold_status": "draft",
            }
        )
    common = {
        "version": "1",
        "space": "llm-engineering-v1",
        "corpus_manifest_hash": hashlib.sha256(
            (ROOT / "corpus_manifests/llm-engineering-v1.yaml").read_bytes()
        ).hexdigest(),
        "source_content_hashes": hashes,
    }
    agent_queries = [
        ("仅根据知识库说明Transformer不用循环网络的原因，首版计划只生成2个任务。", [0], []),
        (
            "比较KVCache与FlashAttention，先生成2个任务，Critic审核后补充一个对比任务再验收。",
            [8, 24],
            ["supplement"],
        ),
        (
            "分析LoRA与Chinchilla的不同目标；若证据缺口构成关键问题，应重新规划而非重复结论。",
            [6, 3],
            [],
        ),
        (
            "先研究KVCache，再基于该结果解释PagedAttention解决的内存问题，明确任务依赖。",
            [24, 10],
            [],
        ),
        ("并行分析DeepSeek-V3和Qwen2.5的模型训练特点，再汇总差异。", [12, 15], []),
        ("根据ReAct论文及多Agent综述撰写引用编号可核验的协作研究报告。", [16, 21], []),
        ("只允许使用所选知识库，说明为什么无法回答今天美元人民币实时汇率，不编造数字。", [], []),
        ("核对RAG能否保证永远没有幻觉，区分论文支持的结论与尚未核实的观点。", [18], []),
        ("研究LoRA的低秩适配原理，需要人工审批计划后才执行研究。", [6], ["approval"]),
        (
            "分析Continuous Batching，保留执行快照供中断后继续，并核验结果和引用。",
            [26],
            ["recovery"],
        ),
    ]
    agent_cases = []
    for index, (query, refs, invariants) in enumerate(agent_queries, 1):
        sources = [source for ref in refs for source in cases[ref].get("expected_sources", [])]
        agent_cases.append(
            {
                "case_id": f"A{index:02}",
                "kind": "agent",
                "query": query,
                "tags": ["workflow", *invariants],
                "answerable": bool(refs),
                "gold_status": "verified" if sources else "draft",
                "expected_sources": sources,
                "required_invariants": invariants,
            }
        )
    return {
        "rag": {
            **common,
            "dataset_id": "llm-rag-v1",
            "name": "LLM知识检索基准（40题）",
            "cases": cases,
        },
        "agent": {
            **common,
            "dataset_id": "llm-agent-v1",
            "name": "Agent研究执行基准（10题）",
            "cases": agent_cases,
        },
    }


if __name__ == "__main__":
    print(json.dumps(compile_datasets(), ensure_ascii=False, indent=2))
