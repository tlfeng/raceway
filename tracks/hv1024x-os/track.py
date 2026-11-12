import json
import os


class KnnParamSource:
    def __init__(self, track, params, **kwargs):
        # choose a suitable index: if there is only one defined for the track
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
            # OpenSearch exact k-NN via painless cosineSimilarity（冒烟验证项：
            # PLAN §七3；OS 的 k-NN 插件接管了绑定，须传 doc['emb']（2026-11-08 冒烟实测，ES 式字段名字符串 class_cast））
            result["body"] = {
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": "cosineSimilarity(params.query, doc['emb']) + 1.0",
                            "params": {"query": self._queries[self._iters]},
                        },
                    }
                },
                "_source": False,
            }
        else:
            # OpenSearch k-NN 查询 DSL（与 ES 的 field/query_vector/num_candidates 不同，
            # 见 PLAN-opensearch-hv1024-三向量对比测试.md §3.2）
            result["body"] = {
                "query": {
                    "knn": {
                        "emb": {
                            "vector": self._queries[self._iters],
                            "k": self._params.get("k", 10),
                        }
                    }
                },
                "_source": False,
            }
            # ef_search 是 HNSW 搜索宽度，不等价于 ES 每分片 num_candidates。
            knn = result["body"]["query"]["knn"]["emb"]
            knn["method_parameters"] = {"ef_search": self._params.get("ef-search", 100)}
            if "oversample-factor" in self._params:
                knn["rescore"] = {"oversample_factor": self._params["oversample-factor"]}
        # Rally search runner 只发送 body，不读取外层 size。
        result["body"]["size"] = self._params.get("k", 10)
        self._iters += 1
        if self._iters >= len(self._queries):
            self._iters = 0
        return result


def register(registry):
    registry.register_param_source("knn-param-source", KnnParamSource)
