# placeholder LLM extractor (mock) for Day 1
import json
def mock_extract(sample):
    """Returns a mocked transaction extraction dictionary for development/testing."""
    return {
        "source_id": sample["id"],
        "amount": 1299.0 if "1299" in sample["body"] else 2000.0,
        "currency": "INR",
        "date": "2025-09-10",
        "merchant": "SampleMerchant",
        "category": "shopping",
        "confidence": 0.9,
        "rationale": "mocked extraction for dev"
    }
if __name__ == "__main__":
    import sys
    s = {"id":"sample_local", "body":"Payment INR 1299.00"}
    print(json.dumps(mock_extract(s), indent=2))
