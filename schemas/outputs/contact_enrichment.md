{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "contact_enrichment",
  "description": "Decision-maker profile inferred from title, bio and public context. MUST NOT generate email addresses, phone numbers or any contact identifier.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "seniority",
    "decision_authority",
    "functional_area",
    "likely_responsibilities",
    "inferred_pains",
    "insufficient_context"
  ],
  "properties": {
    "seniority": {
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
            "founder_owner",
            "c_level",
            "vp",
            "director",
            "manager",
            "ic",
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
    "decision_authority": {
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
            "economic_buyer",
            "champion",
            "influencer",
            "gatekeeper",
            "end_user",
            null
          ],
          "description": "Who they are in a buying process. null when insufficient evidence."
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
    "functional_area": {
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
            "operations",
            "marketing",
            "sales",
            "engineering",
            "finance",
            "people",
            "general_management",
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
    "likely_responsibilities": {
      "type": "array",
      "maxItems": 6,
      "items": {
        "type": "object",
        "required": [
          "responsibility",
          "confidence"
        ],
        "additionalProperties": false,
        "properties": {
          "responsibility": {
            "type": "string",
            "maxLength": 160
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      }
    },
    "inferred_pains": {
      "type": "array",
      "maxItems": 5,
      "items": {
        "type": "object",
        "required": [
          "pain",
          "reasoning",
          "confidence"
        ],
        "additionalProperties": false,
        "properties": {
          "pain": {
            "type": "string",
            "maxLength": 200
          },
          "reasoning": {
            "type": "string",
            "maxLength": 300
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      },
      "description": "Hypotheses only. These are never stated to the prospect as fact."
    },
    "insufficient_context": {
      "type": "boolean"
    }
  }
}
