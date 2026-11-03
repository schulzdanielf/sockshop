from experiment.platform.backend.plugins.mcp_metrics_plugin import (
    MCPPrometheusMetricsPlugin,
)


def test_per_label_hotspots_preserves_http_edge_direction_and_response_code():
    plugin = MCPPrometheusMetricsPlugin()
    raw = {
        "data": {
            "http_5xx_by_edge": {
                "data": {
                    "result": [
                        {
                            "metric": {
                                "source_workload": "front-end",
                                "destination_workload": "orders",
                                "response_code": "500",
                            },
                            "values": [
                                [1790935200, "0"],
                                [1790935320, "0.5"],
                            ],
                        },
                        {
                            "metric": {
                                "source_workload": "payment",
                                "destination_workload": "orders",
                                "response_code": "503",
                            },
                            "values": [
                                [1790935200, "0"],
                                [1790935320, "0.2"],
                            ],
                        },
                    ]
                }
            }
        }
    }
    phases = [
        ("baseline", "2026-10-02T10:00:00Z", "2026-10-02T10:01:00Z"),
        ("fault", "2026-10-02T10:02:00Z", "2026-10-02T10:03:00Z"),
    ]

    result = plugin.per_label_hotspots(raw, phases)["http_5xx_by_edge"]

    assert result["label_kind"] == "edge"
    assert len(result["top_edges"]) == 2
    assert {
        (row["source_workload"], row["destination_workload"], row["response_code"])
        for row in result["top_edges"]
    } == {
        ("front-end", "orders", "500"),
        ("payment", "orders", "503"),
    }
