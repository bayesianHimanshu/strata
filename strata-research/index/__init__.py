"""STRATA index layer: chunking, the vector store, and the leakage filter.

The leakage filter (invariant #2) is enforced *here*, in code, at retrieval time -
not by prompt. A store query that does not declare the decision it retrieves for
cannot be expressed (see store.VectorStore.search requiring a LeakageFilter).
"""
