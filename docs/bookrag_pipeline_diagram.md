# BookRAG Pipeline: Data Structures and Processing Flow

> **Language:** English | [日本語](bookrag_pipeline_diagram_ja.md)

This document describes the current `multi_format_bookrag` implementation in teradataevsui. It focuses on the runtime data flow, persisted Teradata structures, and the node-tree construction algorithm used before VectorStore creation.

## 1. End-to-End Pipeline

```mermaid
flowchart TB
    DOC["Document files"] --> MODE["Multi-Format BookRAG mode"]
    MODE --> DAG["Build inline job_nodes DAG"]
    DAG --> PART["Partitioner<br/>auto / hi_res / vlm / fast"]

    PART -->|No prompters| JOB["Unstructured on-demand job"]
    PART --> ENRICH["Optional ordered prompters<br/>image description / table HTML /<br/>table description / generative OCR / NER"]
    ENRICH --> JOB

    JOB --> RAW_JSON["Raw Unstructured output<br/>JSON elements"]
    RAW_JSON --> RAW_STAGE["Raw stage + parse manifest<br/>uploads/bookrag_raw_stage"]
    RAW_STAGE --> RECON["Reconcile elements<br/>normalize parentage + metadata"]

    RECON --> DOC_ROWS["Build document rows"]
    RECON --> RAW_ROWS["Build audit raw rows"]
    RECON --> BLOCKS["Build normalized blocks"]
    BLOCKS --> NODES["Build document node tree"]
    RECON --> ENTITY_GATE{"Graph tables enabled?"}
    ENTITY_GATE -->|Yes| GRAPH["Build entities + links + relations"]
    ENTITY_GATE -->|No| SKIP_GRAPH["Skip optional entity graph"]
    DOC_ROWS --> DOC_RELS["After all documents<br/>derive / import document relations"]

    DOC_ROWS --> TD[(Teradata BookRAG tables)]
    DOC_RELS --> TD
    RAW_ROWS --> TD
    BLOCKS --> TD
    NODES --> TD
    GRAPH --> TD

    TD --> VIEW["Governed retrieval view<br/>bdoc + bnode + bdrel"]
    TD --> VS["VectorStore source<br/>*_bnode"]
    VIEW --> RETRIEVAL["Governed retrieval<br/>over node content"]
    VS --> RETRIEVAL
```

## 2. Persisted Data Model

```mermaid
erDiagram
    DOCUMENTS ||--o{ RAW : "doc_id"
    DOCUMENTS ||--o{ BLOCKS : "doc_id"
    DOCUMENTS ||--o{ NODES : "doc_id"
    DOCUMENTS ||--o{ ENTITIES : "doc_id"
    DOCUMENTS ||--o{ ENTITY_LINKS : "doc_id"
    DOCUMENTS ||--o{ ENTITY_RELATIONS : "doc_id"
    DOCUMENTS ||--o{ DOCUMENT_RELATIONS : "from_doc_id"
    DOCUMENTS ||--o{ DOCUMENT_RELATIONS : "to_doc_id"

    NODES ||--o{ NODES : "doc_id + parent_node_id"
    BLOCKS ||--o{ NODES : "doc_id + source_element_id"
    BLOCKS ||--o{ ENTITY_RELATIONS : "doc_id + source_element_id"
    NODES ||--o{ ENTITY_LINKS : "doc_id + node_id"
    NODES ||--o{ ENTITY_RELATIONS : "doc_id + source_node_id"
    ENTITIES ||--o{ ENTITY_LINKS : "doc_id + entity_id"
    ENTITIES ||--o{ ENTITY_RELATIONS : "doc_id + from_entity_id"
    ENTITIES ||--o{ ENTITY_RELATIONS : "doc_id + to_entity_id"

    DOCUMENTS {
        string doc_id PK
        string vector_store_name
        string workflow_id
        string workflow_name
        string job_id
        string processing_profile
        string source_file
        string filename
        string filetype
        int filesize_bytes
        int page_count
        string language_hint
        string created_at
        date publication_date
        string publication_date_source
        string publication_date_precision
        string document_series
        string document_role
        string logical_document_key
        int revision_no
        string metadata_status
        string metadata_updated_by
        timestamp metadata_updated_at
    }

    RAW {
        string doc_id PK,FK
        int ordinal_raw PK
        string id
        string element_id
        string parent_id
        string type
        int page_number
        int category_depth
        string text
        string text_as_html
        string image_caption
        string image_context
    }

    BLOCKS {
        string doc_id PK,FK
        string element_id PK
        string parent_id
        int category_depth
        int heading_level
        int page_number
        int ordinal
        string type
        string text
        string text_as_html
        string image_caption
        string image_context
    }

    NODES {
        string doc_id PK,FK
        string node_id PK
        string source_element_id
        string parent_node_id
        string node_type
        int level
        int ordinal
        string title
        string content
        int page_start
        int page_end
        string path
        int is_leaf
    }

    ENTITIES {
        string doc_id PK,FK
        string entity_id PK
        string canonical_name
        string display_name
        string entity_type
        int mention_count
        int node_count
    }

    ENTITY_LINKS {
        string doc_id PK,FK
        string link_id PK
        string entity_id FK
        string node_id FK
        string section_node_id
        string source_field
        string mention_text
        int page_start
        int page_end
        int ordinal
        string section_path
    }

    ENTITY_RELATIONS {
        string doc_id PK,FK
        string relation_id PK
        string source_element_id
        string source_node_id FK
        string section_node_id
        string from_entity_id FK
        string from_entity_text
        string relationship
        string to_entity_id FK
        string to_entity_text
        int page_start
        int page_end
        int ordinal
        string section_path
    }

    DOCUMENT_RELATIONS {
        string from_doc_id PK,FK
        string relation_type PK
        string to_doc_id PK,FK
        string from_filename
        string to_filename
        string relation_description
        string source_type
        string created_by
        timestamp created_at
        string updated_by
        timestamp updated_at
    }
```

## 3. Node-Tree Construction Algorithm

```mermaid
flowchart TD
    A[Reconciled Unstructured elements] --> ROOT[Create document root node<br/>level = 0, is_leaf = 0]
    A --> B[Iterate elements in source order]
    B --> C[Read element fields and metadata<br/>element_id, parent_id, page_number,<br/>category_depth, text_as_html]

    C --> D{Classify block kind}
    D -->|Title or structural section signal| SEC[Section block]
    D -->|Table type or HTML table| TAB[Table block]
    D -->|Image, figure, or picture type| IMG[Image block]
    D -->|Other retained text| TXT[Text block]

    SEC --> LVL[Infer section level<br/>HTML heading, category_depth,<br/>Japanese section rules, numbered headings]
    LVL --> STACK[Update section stack]
    STACK --> SEC_NODE[Create section node<br/>is_leaf = 0]

    TAB --> LEAF_CONTENT[Build leaf content]
    TXT --> LEAF_CONTENT
    IMG --> IMG_CTX[Attach image caption/context<br/>from nearby compatible blocks]
    IMG_CTX --> LEAF_CONTENT

    LEAF_CONTENT --> LONG{Content exceeds<br/>embedding segment size?}
    LONG -->|No| LEAF_NODE[Create one leaf node<br/>is_leaf = 1]
    LONG -->|Yes| SEGMENT[Split into leaf segments<br/>384 token units, 48 overlap]
    SEGMENT --> LEAF_NODE

    ROOT --> NODE_TABLE[(NODES table)]
    SEC_NODE --> NODE_TABLE
    LEAF_NODE --> NODE_TABLE
    NODE_TABLE --> VECTOR[VectorStore retrieval source<br/>key_columns = doc_id, node_id<br/>data_columns = content]
```

## 4. Runtime Object Flow

```mermaid
flowchart TB
    DOC["Document + manifest metadata"] --> DOC_ROW["Document row"]
    ELEM["Unstructured element"] --> RAW["Audit raw row"]
    ELEM --> BLK["Normalized BookRAG block"]
    BLK --> NODE["Document / section / leaf nodes"]
    ELEM -->|Optional NER metadata| ENT["Entity + link + relation rows"]
    DOC_ROW --> DOC_REL["Run-level document relations"]

    DOC_ROW --> DOC_TABLE["*_bdoc"]
    RAW --> RAW_TABLE["*_braw"]
    BLK --> BLOCK_TABLE["*_bblk"]
    NODE --> NODE_TABLE["*_bnode"]
    DOC_REL --> DOC_REL_TABLE["*_bdrel"]
    ENT --> ENTITY_TABLES["*_bent / *_belnk / *_brel"]

    DOC_TABLE --> VIEW["*_retrieval_v"]
    DOC_REL_TABLE --> VIEW
    NODE_TABLE --> VIEW
    NODE_TABLE --> VS_SRC["VectorStore object_names"]
```

## 5. Table Naming Convention

For a vector store named `demo`, BookRAG table targets are generated from the `<vector_store_name>_bk` base name:

```text
demo_bk_bdoc   documents
demo_bk_braw   raw elements
demo_bk_bblk   normalized blocks
demo_bk_bnode  document tree nodes
demo_bk_bdrel  governed document relationships
demo_bk_bent   entities
demo_bk_belnk  entity mentions linked to nodes
demo_bk_brel   entity relations
demo_bk_retrieval_v  governed retrieval view
```

The current BookRAG VectorStore source is the node table:

```text
object_names = <schema>.<vector_store_name>_bk_bnode
key_columns  = ["doc_id", "node_id"]
data_columns = ["content"]
```

Only nodes with retrievable `content` are useful for semantic retrieval. Section nodes preserve hierarchy and path context; leaf nodes carry the text, table, or image-derived content used by the VectorStore.
