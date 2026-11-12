import json
import os


class KnnParamSource:
    def __init__(self, track, params, **kwargs):
        # choose a suitable index: if there is only one defined for this track
        # choose that one, but let the user always override index
        if len(track.indices) == 1:
            default_index = track.indices[0].name
        else:
            default_index = "_all"

        self._index_name = params.get("index", default_index)
        self._cache = params.get("cache", False)
        self._exact_scan = params.get("exact", False)
        self._params = params
        self._queries = []

        cwd = os.path.dirname(__file__)
        with open(os.path.join(cwd, "queries.json"), "r") as file:
            for line in file:
                self._queries.append(json.loads(line))
        self._iters = 0
        self.infinite = True

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        result = {"index": self._index_name, "cache": self._params.get("cache", False)}

        if self._exact_scan:
            result["body"] = {
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query, 'emb') + 1.0",
                            "params": {"query": self._queries[self._iters]},
                        },
                    }
                },
                "_source": False,
            }
        else:
            result["body"] = {
                "knn": {
                    "field": "emb",
                    "query_vector": self._queries[self._iters],
                    "k": self._params.get("k", 10),
                    "num_candidates": self._params.get("num-candidates", 100),
                },
                "_source": False,
            }
        # Rally search runner 只发送 body，不读取外层 size。
        result["body"]["size"] = self._params.get("k", 10)
        self._iters += 1
        if self._iters >= len(self._queries):
            self._iters = 0
        return result


def register(registry):
    registry.register_param_source("knn-param-source", KnnParamSource)
