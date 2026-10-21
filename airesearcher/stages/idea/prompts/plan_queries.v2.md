---
id: idea/plan_queries
version: 2
tier: strong
output_schema: QueryPlan
---
<system>
Write all natural-language output in English. Return only JSON matching the schema.
Split the research requirements into 3 to 5 subquestions covering methods, baselines, and data or feasibility.
Each subquestion requires question, purpose, and 1 to 2 short English keyword queries.
Each query must contain at most 6 whitespace-separated words. Prefer one method name or 2-4 keywords.
Queries are reused across Semantic Scholar and arXiv: do not include Boolean operators AND/OR/NOT,
quotes, parentheses, field prefixes, natural-language questions, or search-engine-specific syntax.
For example use LoRA, parameter efficient fine tuning, or few sample BERT.
Use at least one broad method-name query. Do not make every query require all task and hardware constraints.
Generate queries only, never papers or citations.
Schema: {{ schema_json }}
</system>
<user>
Research requirements:
{{ brief }}
</user>
