from trace_analyzer import extract_features, _parse_spans


def test_error_span_retains_http_code_and_remote_destination():
    trace = {
        "batches": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "orders"}},
            ]},
            "scopeSpans": [{"spans": [{
                "spanId": "client-span", "name": "POST",
                "startTimeUnixNano": "1000000", "endTimeUnixNano": "2000000",
                "status": {"code": "STATUS_CODE_ERROR"},
                "attributes": [
                    {"key": "http.url", "value": {"stringValue": "http://payment/paymentAuth"}},
                    {"key": "http.status_code", "value": {"intValue": "500"}},
                ],
            }]}],
        }],
    }
    features = extract_features(trace)
    span = features["error_spans"][0]
    assert span["service"] == "orders"
    assert span["http_status_code"] == "500"
    assert span["remote_endpoint"] == "payment/paymentAuth"
    assert span["remote_service"] == "payment"
    assert features["failure_signatures"][0]["endpoint"] == "payment/paymentAuth"


def test_current_http_status_attribute_detects_error_without_span_status():
    trace = {"batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{
        "spanId": "client", "name": "POST", "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000", "attributes": [{
            "key": "http.response.status_code", "value": {"intValue": "500"},
        }],
    }]}]}]}
    assert _parse_spans(trace)[0]["is_error"] is True
