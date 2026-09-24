# PRODUCT.md

# AI Second Brain

## Product Specification

---

# 1. Product Summary

AI Second Brain is a persistent memory system for AI agents.

It transforms conversations, documents, and other information into structured, interconnected, evolving knowledge.

Instead of treating every conversation as isolated context, the system builds a long-term memory.

The user can then ask questions about:

* what they know
* what they have done
* what they decided
* what changed
* how concepts are connected
* where information came from
* what information is outdated

---

# 2. Problem

Current AI assistants are excellent at reasoning within a conversation but often have weak long-term structured memory.

Traditional RAG improves retrieval from documents but often treats knowledge as:

```text
chunks → embeddings → similarity
```

This makes it difficult to represent:

* relationships
* temporal changes
* historical states
* entity identity
* contradictions
* multi-hop connections

AI Second Brain explores whether combining:

```text
Persistent Memory
+
Knowledge Graph
+
Vector Retrieval
+
Temporal Reasoning
+
Agentic Tool Use
```

can produce more capable long-term AI behavior.

---

# 3. Product Principle

The system should behave like a second brain.

The user should not need to remember:

> "Which conversation did I mention that in?"

The system should be able to retrieve it.

The user should also be able to inspect:

> "Why does the AI think this?"

and see the underlying memory/source.

---

# 4. Main User Experience

The product has four major experiences:

1. Chat
2. Knowledge Graph
3. Memory Explorer
4. Entity/Source Explorer

---

# 5. Chat

Chat is the primary interface.

Example:

> What projects have I worked on involving OCR?

The system should search memory and answer using relevant graph/vector context.

Another:

> What was I using for OCR in March?

The system should use temporal memory.

Another:

> What did I decide about the project architecture?

The system should locate relevant decision memories.

---

# 6. Knowledge Graph

The user can visualize their knowledge.

Example:

```text
                 Gemini
                    |
                    |
             Drawing Extractor
              /      |       \
             /       |        \
        EasyOCR    OpenCV   Cutlist
             |
             |
            OCR
```

The graph should support:

* zoom
* pan
* search
* node selection
* relationship inspection
* expansion
* filtering
* temporal views

---

# 7. Memory Explorer

The user can inspect stored memory.

Example:

```text
Memory

"Project uses EasyOCR"

Status:
Historical

Valid:
January → May

Source:
Conversation #182

Confidence:
0.92
```

---

# 8. Entity Explorer

Each entity gets a page.

Example:

```text
Drawing Extractor

Type:
Project

Current technologies:
Gemini
PaddleOCR
OpenCV

Historical technologies:
EasyOCR

Related projects:
Cutlist Optimizer

Recent changes:
OCR library changed

Sources:
3 conversations
2 documents
```

---

# 9. Memory Evolution

Example interaction:

Initial:

> We use EasyOCR.

Memory:

```text
Project → uses → EasyOCR
```

Later:

> We switched to PaddleOCR.

Memory becomes:

```text
Project → previously_used → EasyOCR
Project → currently_uses → PaddleOCR
```

The old memory remains available historically.

---

# 10. User Questions

The system should support questions like:

### Memory

> What do I know about X?

### Projects

> Which projects use Python?

### Relationships

> How are X and Y connected?

### Temporal

> What was I working on in March?

### Historical

> What did I use before PaddleOCR?

### Decisions

> What decisions did I make about this project?

### Provenance

> Where did I mention this?

### Contradictions

> Do I have conflicting information about this?

---

# 11. Document Learning

Users should eventually be able to upload:

* PDFs
* Markdown
* TXT
* DOCX

The system learns relevant structured information from them.

Documents become sources of memory.

---

# 12. Agent Experience

The agent should use memory intentionally.

For example:

```text
User:
"What did I decide about the project architecture?"

Agent:
→ Search memory
→ Find project
→ Search decisions
→ Retrieve temporal context
→ Retrieve sources
→ Answer
```

---

# 13. Memory Transparency

Whenever practical, answers should expose:

* supporting memories
* source documents
* source conversations
* relevant dates

The goal is not just:

> "Trust the AI."

It is:

> "Here is why the AI knows this."

---

# 14. Product Differentiator

The project differentiates itself through:

### Persistent memory

Information survives conversations.

### Temporal memory

The system understands change.

### Graph structure

The system understands relationships.

### Hybrid retrieval

The system combines semantic and structural retrieval.

### Agent tools

The agent can interact with memory.

### Evaluation

The system measures whether these techniques actually improve performance.

---

# 15. MVP

The MVP must include:

* chat
* memory extraction
* Neo4j graph
* graph visualization
* persistent memory
* basic retrieval
* source references

The MVP does NOT need:

* voice
* mobile application
* dozens of integrations
* autonomous background agents
* complicated multi-agent systems

---

# 16. V1

V1 should include:

* authentication
* persistent conversations
* graph memory
* vector retrieval
* hybrid retrieval
* entity resolution
* temporal memory
* conflict handling
* provenance
* agent tools
* document ingestion
* evaluation
* deployment

---

# 17. Future Product Directions

Potential future capabilities:

* GitHub memory
* Notion memory
* Calendar memory
* Email memory
* browser memory
* multimodal memory
* voice
* organizational memory
* shared agent memory
* agent-specific memory
* autonomous memory consolidation

These are future possibilities, not V1 requirements.

---

# 18. Product Success

The product succeeds if a user can interact with the AI over a long period and feel:

> "It actually remembers how things evolved."

That is more important than the number of graph nodes or UI animations.
