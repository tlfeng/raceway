import copy
import json
import os
import random

from esrally import exceptions


class UpsertParamSource:
    """按自然 geonameid 顺序生成 index / upsert / update bulk。

    每阶段遍历同一语料切片一次。raceway_revision 记录阶段（0/1/2），
    hot 与 native-update 使用相同的 revision=1；更新显式关闭 no-op 检测，
    因而单独重跑 hot/mixed 也是真实重写，而不是吞吐很高的 NOOP。
    """

    def __init__(self, track, params, **kwargs):
        self._params = params
        self._track = track
        self._index = params.get("index") or track.indices[0].name
        self._bulk_size = int(params.get("bulk-size", 500))
        self._ingest_percentage = float(params.get("ingest-percentage", 100))
        self._new_doc_ratio = float(params.get("new-doc-ratio", 0))
        self._new_id_offset = int(params.get("new-id-offset", 1000000000))
        self._seed = int(params.get("upsert-seed", 42))
        self._action = params.get("action", "upsert")
        self._revision = int(params.get("revision", 0))
        if self._bulk_size <= 0 or not 0 < self._ingest_percentage <= 100:
            raise exceptions.SystemError("bulk-size 必须大于 0，ingest-percentage 必须在 (0, 100] 内")
        if not 0 <= self._new_doc_ratio <= 100 or self._new_id_offset <= 0:
            raise exceptions.SystemError("new-doc-ratio 必须在 [0, 100] 内，new-id-offset 必须大于 0")
        if self._action not in ("index", "upsert", "update") or self._revision < 0:
            raise exceptions.SystemError("action 必须是 index/upsert/update，revision 必须非负")
        if self._action != "upsert" and self._new_doc_ratio:
            raise exceptions.SystemError("只有 upsert 模式允许 new-doc-ratio")
        self._random = random.Random(self._seed)
        self._partition_index = 0
        self._total_partitions = 1
        self.infinite = False
        self._docs_emitted = 0
        self._target_docs = None
        # Rally 加载赛道时语料可能尚未下载；到首次 params() 再定位。
        self._corpus_file = None
        self._corpus_docs = None
        self._requests = None
        # 暴露 corpora，保证只有自定义参数源的 challenge 也会触发 Rally 数据准备。
        track_corpora_names = [corpus.name for corpus in track.corpora]
        corpora_names = params.get("corpora", track_corpora_names)
        if isinstance(corpora_names, str):
            corpora_names = [corpora_names]
        self.corpora = [corpus for corpus in track.corpora if corpus.name in corpora_names]

    def _resolve_corpus(self):
        """返回路径及声明条数；显式 corpus-file 的条数从文件统计。"""
        explicit = self._params.get("corpus-file")
        if explicit is not None:
            return explicit, None
        for corpus in self.corpora:
            for doc_set in corpus.documents:
                path = doc_set.document_file
                if path and os.path.exists(path):
                    return path, doc_set.number_of_documents
        return None, None

    def _ensure_corpus(self):
        if self._corpus_docs is not None:
            return
        path, num_docs = self._resolve_corpus()
        if path is None or not os.path.exists(path):
            raise exceptions.SystemError(
                "找不到 geonames 语料；请先让 Rally 下载解压，或用 corpus_file 指定 documents-2.json"
            )
        if num_docs is None:
            with open(path, "rb") as f:
                num_docs = sum(1 for _ in f)
        if num_docs <= 0:
            raise exceptions.SystemError("geonames 语料不能为空")
        self._corpus_file, self._corpus_docs = path, num_docs

    def partition(self, partition_index, total_partitions):
        if total_partitions <= 0 or not 0 <= partition_index < total_partitions:
            raise exceptions.SystemError("非法的客户端分区")
        p = copy.copy(self)
        p._partition_index = partition_index
        p._total_partitions = total_partitions
        p._random = random.Random(self._seed * 1000003 + partition_index)
        p._docs_emitted = 0
        p._target_docs = None
        p._requests = None
        return p

    @property
    def percent_completed(self):
        if self._target_docs is None:
            return None
        return min(self._docs_emitted / self._target_docs, 1.0)

    def _slice(self, num_target_docs):
        per_partition = num_target_docs // self._total_partitions
        if per_partition == 0:
            raise exceptions.SystemError(
                f"客户端数 ({self._total_partitions}) 超过目标文档数 ({num_target_docs})"
            )
        start = self._partition_index * per_partition
        end = start + per_partition
        if self._partition_index == self._total_partitions - 1:
            end = num_target_docs
        return start, end

    def _bulk_params(self, batch):
        # 保留 request-timeout / detailed-results 等 runner 参数。
        result = dict(self._params)
        result.update({
            "body": batch,
            "bulk-size": len(batch) // 2,
            "unit": "docs",
            "action-metadata-present": True,
        })
        return result

    def params(self):
        if self._requests is None:
            self._requests = self._build_requests()
        return next(self._requests)

    def _build_requests(self):
        self._ensure_corpus()
        num_target_docs = max(1, int(round(self._corpus_docs * self._ingest_percentage / 100.0)))
        start, end = self._slice(num_target_docs)
        self._target_docs = end - start
        batch = []
        with open(self._corpus_file, "rb") as f:
            for line_no, line in enumerate(f):
                if line_no < start:
                    continue
                if line_no >= end:
                    break
                try:
                    doc = json.loads(line)
                    doc_id = doc["geonameid"]
                    if type(doc_id) is not int or doc_id < 0:
                        raise ValueError("geonameid 必须为非负整数")
                except (ValueError, KeyError, TypeError) as e:
                    raise exceptions.SystemError(f"语料第 {line_no + 1} 行无效: {e}") from e
                if self._new_doc_ratio and self._random.random() * 100.0 < self._new_doc_ratio:
                    doc_id += self._new_id_offset
                doc["raceway_revision"] = self._revision
                action = "index" if self._action == "index" else "update"
                metadata = {action: {"_index": self._index, "_id": str(doc_id)}}
                if self._action == "index":
                    body = doc
                else:
                    body = {"doc": doc, "detect_noop": False}
                    if self._action == "upsert":
                        body["doc_as_upsert"] = True
                batch.append(json.dumps(metadata, separators=(",", ":")).encode("utf-8"))
                batch.append(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if len(batch) >= self._bulk_size * 2:
                    self._docs_emitted += len(batch) // 2
                    yield self._bulk_params(batch)
                    batch = []
        if batch:
            self._docs_emitted += len(batch) // 2
            yield self._bulk_params(batch)
        if self._docs_emitted != self._target_docs:
            raise exceptions.SystemError("语料提前结束，实际条数与声明不符")


def register(registry):
    registry.register_param_source("upsert-source", UpsertParamSource)
