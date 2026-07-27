{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "company_enrichment",
  "description": "Structured company profile inferred from provided research context. Model supplies value/confidence/evidence only; source, run_id and observed_at are added by code.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "industry",
    "sub_industry",
    "business_model",
    "employee_band",
    "revenue_signal",
    "positioning_summary",
    "tech_signals",
    "insufficient_context"
  ],
  "properties": {
    "industry": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "type": [
            "string",
            "null"
          ],
          "maxLength": 200,
          "description": "null when evidence is insufficient. Never guess."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400,
          "description": "Verbatim or near-verbatim snippet from the provided context that supports this value. Empty string only when value is null."
        }
      }
    },
    "sub_industry": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "type": [
            "string",
            "null"
          ],
          "maxLength": 200,
          "description": "null when evidence is insufficient. Never guess."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400,
          "description": "Verbatim or near-verbatim snippet from the provided context that supports this value. Empty string only when value is null."
        }
      }
    },
    "business_model": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "enum": [
            "b2b_saas",
            "b2b_services",
            "agency",
            "ecommerce",
            "marketplace",
            "coaching_education",
            "nonprofit",
            "other",
            null
          ],
          "description": " null when insufficient evidence."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400
        }
      }
    },
    "employee_band": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "enum": [
            "1-10",
            "11-50",
            "51-200",
            "201-1000",
            "1000+",
            null
          ],
          "description": " null when insufficient evidence."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400
        }
      }
    },
    "revenue_signal": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "enum": [
            "pre_revenue",
            "early",
            "growing",
            "established",
            "unknown",
            null
          ],
          "description": " null when insufficient evidence."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400
        }
      }
    },
    "positioning_summary": {
      "type": "object",
      "required": [
        "value",
        "confidence",
        "evidence"
      ],
      "additionalProperties": false,
      "properties": {
        "value": {
          "type": [
            "string",
            "null"
          ],
          "maxLength": 500,
          "description": "What they sell and to whom, in their own framing. Not marketing copy."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        },
        "evidence": {
          "type": "string",
          "maxLength": 400
        }
      }
    },
    "tech_signals": {
      "type": "array",
      "maxItems": 10,
      "items": {
        "type": "object",
        "required": [
          "name",
          "evidence"
        ],
        "additionalProperties": false,
        "properties": {
          "name": {
            "type": "string",
            "maxLength": 80
          },
          "evidence": {
            "type": "string",
            "maxLength": 300
          }
        }
      }
    },
    "insufficient_context": {
      "type": "boolean",
      "description": "true when the provided context does not support any confident inference. Prefer this over guessing."
    }
  }
}
