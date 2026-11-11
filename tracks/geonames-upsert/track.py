import copy
import os
import random
import re

from esrally import exceptions

GEONAMEID_PATTERN = re.compile(rb'"geonameid":\s*(\d+)')

ACTION_LINE_TEMPLATE = b'{"update": {"_index": "%s", "_id": "%s"}}'
UPSERT_BODY_LINE_TEMPLATE = b'{"doc":%s,"doc_as_upsert":true}'


class UpsertParamSource:
    """
    Streams the geonames corpus and emits raw bulk bodies of update actions:

        {"update": {"_index": "<index>", "_id": "<id>"}}
        {"doc":<original document>,"doc_as_upsert":true}

    Document ids are the natural ``geonameid`` of each corpus document. With
    ``new-doc-ratio`` > 0, that percentage of the operations targets new ids
    (geonameid + ``new-id-offset``) so the upsert takes the insert path,
    while the rest target existing ids and take the update path.
    """

    def __init__(self, track, params, **kwargs):
        self._params = params
        self._track = track
        self._index = params.get("index") or track.indices[0].name
        self._bulk_size = int(params.get("bulk-size", 500))
        self._ingest_percentage = min(float(params.get("ingest-percentage", 100)), 100.0)
        self._new_doc_ratio = max(float(params.get("new-doc-ratio", 0)), 0.0)
        self._new_id_offset = int(params.get("new-id-offset", 1000000000))
        self._seed = int(params.get("upsert-seed", 42))
        self._random = random.Random(self._seed)
        self._partition_index = 0
        self._total_partitions = 1
        self.infinite = False
        self._docs_emitted = 0
        # Corpus resolution is deferred to params(): Rally constructs param sources during
        # track load, before the corpus is downloaded. Failing hard here would deadlock
        # the data preparation.
        self._corpus_file = None
        self._corpus_docs = None
        self._requests = None
        # Expose the corpora so Rally's track data preparation downloads and extracts them
        # even for challenges that only use this custom param source. Selection mirrors
        # BulkIndexParamSource.used_corpora but without importing Rally internals, so it
        # works across Rally versions (loader only needs .name and .union on these).
        track_corpora_names = [corpus.name for corpus in track.corpora]
        corpora_names = params.get("corpora", track_corpora_names)
        if isinstance(corpora_names, str):
            corpora_names = [corpora_names]
        self.corpora = [corpus for corpus in track.corpora if corpus.name in corpora_names]

    def _resolve_corpus(self):
        """Return (path, num_docs). ``num_docs`` is None when it must be counted from the file."""
        explicit = self._params.get("corpus-file")
        if explicit is not None:
            return explicit, None
        for corpus in self._track.corpora:
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
                "Could not locate the geonames corpus document file. Run the track once via Rally so the "
                "corpus is downloaded and extracted, or point the 'corpus-file' parameter at the extracted "
                "documents-2.json."
            )
        if num_docs is None:
            with open(path, "rb") as f:
                num_docs = sum(1 for _ in f)
        self._corpus_file, self._corpus_docs = path, num_docs

    def partition(self, partition_index, total_partitions):
        p = copy.copy(self)
        p._partition_index = partition_index
        p._total_partitions = total_partitions
        p._random = random.Random(self._seed * 1000003 + partition_index)
        p._docs_emitted = 0
        return p

    @property
    def percent_completed(self):
        if self._corpus_docs is None:
            return None
        num_target_docs = max(1, int(round(self._corpus_docs * self._ingest_percentage / 100.0)))
        return min(self._docs_emitted / num_target_docs, 1.0)

    def _slice(self, num_target_docs):
        per_partition = num_target_docs // self._total_partitions
        if per_partition == 0:
            raise exceptions.SystemError(
                f"Too many clients ({self._total_partitions}) for the requested document count "
                f"({num_target_docs}). Reduce the number of clients or raise ingest-percentage."
            )
        start = self._partition_index * per_partition
        end = start + per_partition
        if self._partition_index == self._total_partitions - 1:
            end = num_target_docs
        return start, end

    def _bulk_params(self, batch):
        return {
            "body": batch,
            "bulk-size": len(batch) // 2,
            "unit": "docs",
            "action-metadata-present": True,
        }

    def params(self):
        # Rally's schedule loop calls params() once per bulk request and expects a single
        # request dict; StopIteration signals that the source is exhausted.
        if self._requests is None:
            self._requests = self._build_requests()
        try:
            return next(self._requests)
        except StopIteration:
            self._requests = None
            raise

    def _build_requests(self):
        self._ensure_corpus()
        num_target_docs = max(1, int(round(self._corpus_docs * self._ingest_percentage / 100.0)))
        start, end = self._slice(num_target_docs)
        batch = []
        with open(self._corpus_file, "rb") as f:
            for line_no, line in enumerate(f):
                if line_no < start:
                    continue
                if line_no >= end:
                    break
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                match = GEONAMEID_PATTERN.search(line)
                if match is None:
                    raise exceptions.SystemError(f"Cannot extract geonameid from corpus line [{line_no}]")
                doc_id = int(match.group(1))
                if self._new_doc_ratio and self._random.random() * 100.0 < self._new_doc_ratio:
                    doc_id += self._new_id_offset
                batch.append(ACTION_LINE_TEMPLATE % (self._index.encode("utf-8"), str(doc_id).encode("utf-8")))
                batch.append(UPSERT_BODY_LINE_TEMPLATE % line)
                if len(batch) >= self._bulk_size * 2:
                    self._docs_emitted += self._bulk_size
                    yield self._bulk_params(batch)
                    batch = []
        if batch:
            self._docs_emitted += len(batch) // 2
            yield self._bulk_params(batch)


def register(registry):
    registry.register_param_source("upsert-source", UpsertParamSource)
