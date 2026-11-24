# Offline cooperation reference tokenizer

`cooperation-tokenizer.json` is a small synthetic BPE tokenizer trained locally
with tokenizers 0.23.3. The public training texts were `NEUTRAL` and the default
announced rule paragraph for a standing of 20/20, no sanction, weight 1. It uses
ByteLevel pre-tokenization, the complete byte alphabet, `[UNK]`, and a requested
vocabulary size of 1,200. It contains no episode content or private annotations.
The committed JSON bytes, rather than a retraining result, are the pinned asset;
merge tie ordering may vary on retraining.

This fixture tests actual local tokenization and padding behavior. It is **not**
a tokenizer for any roster provider. The golden paragraphs in
`cooperation-prompts.json` use this asset, a standing of 2/5, five known checks,
`no_review`, weight 1/5, and the provisional 1,024-token budget. Their audit pins
the tokenizer hash. No provider-specific matching claim follows from this test.
