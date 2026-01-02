"""Simple analysis runner for dev stage."""
import pandas as pd

def demo():
    """Demonstrates basic pandas groupby aggregation on dummy expense data."""
    df = pd.DataFrame([
        {"amount": 1299.0, "category": "shopping", "date": "2025-09-10"},
        {"amount": 2000.0, "category": "bills", "date": "2025-08-31"},
    ])
    print(df.groupby("category").amount.sum())

if __name__ == "__main__":
    demo()
