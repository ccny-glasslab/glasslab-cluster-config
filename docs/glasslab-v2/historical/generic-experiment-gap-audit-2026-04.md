# Generic Experiment Endpoint Surface Audit

Date: 2026-04-22

## Executive Summary

This audit examines the `workflow-api` endpoint surface in `/Users/glasslab/cluster-config/services/workflow-api` against the implied generic experiment contract derived from Glasslab v2 documentation.

**Key Finding**: The current endpoint surface is **heavily specialized** and lacks clear mapping to generic reusable experiment phases. Most endpoints are session-specific or tied to legacy autoresearch workflows, making them unsuitable for generic experiment orchestration.

**No endpoints were modified** during this audit. This document is purely diagnostic.

---

## Generic Experiment Contract (Derived)

Based on `command-surface-spec.md`, `overview.md`, and `router-and-backend-contract.md`, the generic experiment lifecycle should support:

| Phase | Purpose | Generic Endpoints |
|-------|---------|-------------------|
| **Run Creation** | Launch validated experiment | `POST /runs` |
| **Preflight** | Validate run readiness | `GET /preflight` |
| **Compare** | Compare run results | `GET /runs/comparison` |
| **Decisions** | Record human decisions | `POST /decisions` |
| **Result Ingestion** | Ingest and surface results | `GET /runs/{id}/results` |

---

## Current Endpoint Audit

### 1. Run Creation Endpoints

| Current Endpoint | Method | Category | Generic? | Gap |
|------------------|--------|----------|----------|-----|
| `POST /runs` | POST | Generic ✅ | **YES** | **None** |
| `POST /runs/from-latest-design-draft` | POST | Session-specific | ❌ | Should be `POST /runs/from-design/{design_id}` |
| `POST /research-sessions/{session_id}/runs/from-design` | POST | Session-specific | ❌ | Redundant with above, adds session coupling |
| `POST /research-sessions/latest/runs/from-design` | POST | Session-specific | ❌ | Uses "latest" anti-pattern |

**Analysis**:
- Only `POST /runs` is truly generic
- Session-specific run creation endpoints obscure the generic interface
- `POST /runs/from-latest-design-draft` duplicates functionality with unnecessary session coupling
- No standardized path for creating runs from arbitrary design IDs

**Desired Mapping**:
```
POST /runs                          → Generic run creation
POST /runs/from-design/{design_id}  → Generic run from design (NEW)
GET  /designs/{design_id}/preflight → Preflight check on design (NEW)
```

---

### 2. Preflight Endpoints

| Current Endpoint | Method | Category | Generic? | Gap |
|------------------|--------|----------|----------|-----|
| `GET /workflow-families/{workflow_id}/execution-preflight` | GET | Session-specific | ❌ | Tied to workflow, not design |
| `GET /research-sessions/latest/execution-preflight` | GET | Session-specific | ❌ | Uses "latest" anti-pattern |
| `GET /research-sessions/{session_id}/execution-preflight` | GET | Session-specific | ❌ | Session coupling |

**Analysis**:
- Preflight endpoints are all tied to sessions, not designs
- No standalone preflight for design artifacts
- Missing: preflight on design without session context

**Desired Mapping**:
```
GET /preflight/design/{design_id}           → Design-specific preflight (NEW)
GET /preflight/run/{run_id}                  → Run-specific preflight (NEW)
GET /preflight/workflow/{workflow_id}        → Workflow preflight (generic)
```

---

### 3. Compare Endpoints

| Current Endpoint | Method | Category | Generic? | Gap |
|------------------|--------|----------|----------|-----|
| `GET /autoresearch/campaigns/{campaign_id}/model-comparison` | GET | Session-specific | ❌ | Campaign-specific, autoresearch-only |
| `GET /research-sessions/{session_id}/autoresearch-model-comparison` | GET | Session-specific | ❌ | Session coupling |
| `GET /research-sessions/latest/autoresearch-model-comparison` | GET | Session-specific | ❌ | "latest" anti-pattern |

**Analysis**:
- **NO GENERIC COMPARE ENDPOINT EXISTS**
- All comparison endpoints are autoresearch/campaign-specific
- Comparison logic is buried in autoresearch campaign state
- Missing: compare arbitrary runs by ID
- Missing: compare runs without campaign context

**Desired Mapping**:
```
GET /runs/comparison?runs=id1,id2,id3        → Compare arbitrary runs (NEW)
GET /runs/{run_id}/comparisons               → List comparisons for run (NEW)
GET /comparisons/{comparison_id}             → Get comparison by ID (NEW)
```

---

### 4. Decision Endpoints

| Current Endpoint | Method | Category | Generic? | Gap |
|------------------|--------|----------|----------|-----|
| `POST /research-sessions/{session_id}/decisions/current` | POST | Session-specific | ❌ | Session coupling |
| `POST /research-sessions/latest/decisions/current` | POST | Session-specific | ❌ | "latest" anti-pattern |
| `POST /autoresearch/campaigns/{campaign_id}/decide-latest` | POST | Session-specific | ❌ | Autoresearch-specific |
| `POST /autoresearch/campaigns/{campaign_id}/decide-ready-batch` | POST | Session-specific | ❌ | Autoresearch-specific |

**Analysis**:
- Decisions are tied to sessions or autoresearch campaigns
- No generic decision recording for arbitrary runs
- Missing: record decision on run without session context

**Desired Mapping**:
```
POST /decisions                                → Generic decision record (NEW)
POST /decisions/batch                          → Batch decision record (NEW)
POST /runs/{run_id}/decisions                  → Decision on specific run (NEW)
GET  /decisions?run_id={run_id}                → List decisions for run (NEW)
GET  /decisions/{decision_id}                  → Get decision by ID (generic)
```

---

### 5. Result Ingestion Endpoints

| Current Endpoint | Method | Category | Generic? | Gap |
|------------------|--------|----------|----------|-----|
| `GET /runs/{run_id}/artifacts` | GET | Generic ✅ | **YES** | **None** |
| `GET /runs/{run_id}/logs` | GET | Generic ✅ | **YES** | **None** |
| `GET /runs` | GET | Session-specific | ❌ | Returns all runs without filtering |
| `GET /research-sessions/{session_id}/runs` | GET | Session-specific | ❌ | Session coupling |
| `GET /research-sessions/latest/runs` | GET | Session-specific | ❌ | "latest" anti-pattern |

**Analysis**:
- Artifact and log endpoints are generic ✅
- Run listing endpoints are session-specific
- No generic way to list runs by status, workflow, or date range
- Missing: search/runs by criteria

**Desired Mapping**:
```
GET  /runs                                    → List runs (generic, filterable) (IMPROVE)
GET  /runs?status={status}                    → Filter by status (NEW)
GET  /runs?workflow_id={workflow_id}          → Filter by workflow (NEW)
GET  /runs?created_after={timestamp}          → Filter by date (NEW)
GET  /runs/search?q={query}                   → Search runs (NEW)
```

---

## Endpoints That Are Too Specific / Legacy

These endpoints appear to be legacy or overly specific:

| Endpoint | Category | Recommendation |
|----------|----------|----------------|
| `POST /paper-pipelines/fresh-paper` | Legacy | Deprecate or move to literature service |
| `POST /paper-pipelines/from-research-problem` | Legacy | Deprecate or move to literature service |
| All `autoresearch/*` endpoints | Session-specific | Refactor to generic run comparison |
| All `digest-schedules/*` endpoints | Legacy | Deprecate or move to scheduler service |
| All `approved-rerun-schedules/*` endpoints | Session-specific | Simplify or deprecate |
| `POST /design-drafts/{design_id}/review` | Session-specific | Should be generic design update |
| `POST /design-drafts/latest/review` | Session-specific | Should be generic design update |

---

## Gap Summary Table

| Gap Type | Generic Endpoint | Current Mapping | Issue |
|----------|------------------|-----------------|-------|
| **Run Creation** | `POST /runs/from-design/{design_id}` | `POST /runs/from-latest-design-draft` | Session coupling |
| **Preflight** | `GET /preflight/design/{design_id}` | `GET /research-sessions/{session_id}/execution-preflight` | Session coupling |
| **Compare** | `GET /runs/comparison` | (NONE) | Missing endpoint |
| **Compare** | `GET /runs/{run_id}/comparisons` | (NONE) | Missing endpoint |
| **Decisions** | `POST /decisions` | `POST /research-sessions/{session_id}/decisions/current` | Session coupling |
| **Decisions** | `POST /runs/{run_id}/decisions` | (NONE) | Missing endpoint |
| **Run Listing** | `GET /runs?status={status}` | `GET /runs` (unfiltered) | Missing filters |
| **Run Listing** | `GET /runs?workflow_id={workflow_id}` | `GET /runs` (unfiltered) | Missing filters |

---

## Detailed Endpoint Inventory

### Run Creation (2 generic, 3 session-specific)

**Generic:**
- ✅ `POST /runs` - Generic run creation
- ⚠️ `POST /runs/from-latest-design-draft` - Should be generic, not latest-specific

**Session-Specific:**
- ❌ `POST /research-sessions/{session_id}/runs/from-design`
- ❌ `POST /research-sessions/latest/runs/from-design`
- ❌ `POST /research-sessions/latest/runs/from-design` (duplicate)

**Recommendation**: Consolidate to `POST /runs/from-design/{design_id}`

---

### Preflight (3 session-specific, 1 generic workflow-only)

**Generic:**
- ✅ `GET /workflow-families/{workflow_id}/execution-preflight` - Workflow preflight

**Session-Specific:**
- ❌ `GET /research-sessions/latest/execution-preflight`
- ❌ `GET /research-sessions/{session_id}/execution-preflight`

**Recommendation**: Add `GET /preflight/design/{design_id}` and deprecate session-specific paths

---

### Compare (3 session-specific/autoresearch-only, 0 generic)

**Autoresearch-Specific (not generic):**
- ❌ `GET /autoresearch/campaigns/{campaign_id}/model-comparison`
- ❌ `GET /research-sessions/{session_id}/autoresearch-model-comparison`
- ❌ `GET /research-sessions/latest/autoresearch-model-comparison`

**Missing (Generic):**
- ❌ No generic compare endpoint exists
- ❌ No way to compare arbitrary runs by ID

**Recommendation**: Create `GET /runs/comparison?runs={run_id,...}`

---

### Decisions (4 session-specific/autoresearch-specific, 0 generic)

**Session/Autoresearch-Specific:**
- ❌ `POST /research-sessions/{session_id}/decisions/current`
- ❌ `POST /research-sessions/latest/decisions/current`
- ❌ `POST /autoresearch/campaigns/{campaign_id}/decide-latest`
- ❌ `POST /autoresearch/campaigns/{campaign_id}/decide-ready-batch`

**Missing (Generic):**
- ❌ No generic decision recording endpoint
- ❌ No way to record decision on arbitrary run

**Recommendation**: Create `POST /decisions` with run_id optional, `POST /runs/{run_id}/decisions`

---

### Result Ingestion (2 generic, 3 session-specific)

**Generic:**
- ✅ `GET /runs/{run_id}/artifacts`
- ✅ `GET /runs/{run_id}/logs`

**Session-Specific:**
- ❌ `GET /runs` (unfiltered)
- ❌ `GET /research-sessions/{session_id}/runs`
- ❌ `GET /research-sessions/latest/runs`

**Recommendation**: Make `GET /runs` generic with filters, deprecate session-specific run listing

---

## Recommended Endpoint Renaming/Restructuring

### Phase 1: Run Creation
```
# Current (session-specific)
POST /runs/from-latest-design-draft
POST /research-sessions/{session_id}/runs/from-design
POST /research-sessions/latest/runs/from-design

# New (generic)
POST /runs/from-design/{design_id}
```

### Phase 2: Preflight
```
# Current (session-specific)
GET /research-sessions/{session_id}/execution-preflight
GET /research-sessions/latest/execution-preflight

# New (generic)
GET /preflight/design/{design_id}
GET /preflight/run/{run_id}
GET /preflight/workflow/{workflow_id}  # Already exists
```

### Phase 3: Compare (NEW ENDPOINTS)
```
# Missing (generic)
GET /runs/comparison?runs={run_id,...}
GET /runs/{run_id}/comparisons
GET /comparisons/{comparison_id}
```

### Phase 4: Decisions (NEW ENDPOINTS)
```
# Current (session-specific)
POST /research-sessions/{session_id}/decisions/current
POST /autoresearch/campaigns/{campaign_id}/decide-latest

# New (generic)
POST /decisions
POST /decisions/batch
POST /runs/{run_id}/decisions
GET  /decisions?run_id={run_id}
GET  /decisions/{decision_id}
```

### Phase 5: Result Ingestion (IMPROVE)
```
# Current (unfiltered)
GET /runs
GET /research-sessions/{session_id}/runs
GET /research-sessions/latest/runs

# New (generic with filters)
GET /runs?status={status}
GET /runs?workflow_id={workflow_id}
GET /runs?created_after={timestamp}
GET /runs/search?q={query}
```

---

## Autoresearch Endpoints Status

The autoresearch endpoint family is **not generic** and should be treated as:

1. **Session-specific workflows** (currently the primary use)
2. **Legacy campaign-based experimentation**
3. **Not suitable for generic experiment orchestration**

If autoresearch is needed, it should be:
- Accessed through session endpoints only
- Or moved to a separate `autoresearch-api` service
- Or replaced with generic compare/decision endpoints

---

## Conclusion

### Current State

| Endpoint Category | Generic | Session-Specific | Too Specific/Legacy |
|-------------------|---------|------------------|---------------------|
| Run Creation | 1 | 3 | 0 |
| Preflight | 1 | 3 | 0 |
| Compare | 0 | 3 | 0 |
| Decisions | 0 | 4 | 0 |
| Result Ingestion | 2 | 3 | 0 |
| **TOTAL** | **4** | **16** | **0** |

### Key Gaps

1. **No generic compare endpoint** - Critical missing functionality
2. **No generic decision endpoint** - Decisions tied to sessions/campaigns
3. **Session coupling in run creation** - Should accept design_id directly
4. **Preflight tied to sessions** - Should support standalone design preflight
5. **Unfiltered run listing** - Should support search/filters

### Priority Actions

1. **HIGH**: Create `GET /runs/comparison` endpoint
2. **HIGH**: Create `POST /decisions` endpoint  
3. **MEDIUM**: Create `GET /preflight/design/{design_id}` endpoint
4. **MEDIUM**: Refactor run creation to accept `design_id` parameter
5. **LOW**: Deprecate autoresearch-specific compare/decision endpoints

---

## Appendix: Endpoint Mapping Reference

### Current Endpoint → Desired Mapping

| Current | Desired | Status |
|---------|---------|--------|
| `POST /runs` | `POST /runs` | ✅ Keep |
| `POST /runs/from-latest-design-draft` | `POST /runs/from-design/{design_id}` | 🔄 Rename |
| `POST /research-sessions/{session_id}/runs/from-design` | `POST /runs/from-design/{design_id}` | 🔄 Consolidate |
| `POST /research-sessions/latest/runs/from-design` | `POST /runs/from-design/{design_id}` | 🔄 Consolidate |
| `GET /workflow-families/{workflow_id}/execution-preflight` | `GET /preflight/workflow/{workflow_id}` | ✅ Keep |
| `GET /research-sessions/{session_id}/execution-preflight` | `GET /preflight/design/{design_id}` | 🔄 Rename |
| `GET /research-sessions/latest/execution-preflight` | `GET /preflight/design/{design_id}` | 🔄 Rename |
| `GET /autoresearch/campaigns/{campaign_id}/model-comparison` | `GET /runs/comparison?campaign_id={campaign_id}` | 🔄 Deprecate |
| `GET /research-sessions/{session_id}/autoresearch-model-comparison` | `GET /runs/comparison?session_id={session_id}` | 🔄 Deprecate |
| `GET /research-sessions/latest/autoresearch-model-comparison` | `GET /runs/comparison` | 🔄 Deprecate |
| `POST /research-sessions/{session_id}/decisions/current` | `POST /decisions` | 🔄 Rename |
| `POST /research-sessions/latest/decisions/current` | `POST /decisions` | 🔄 Rename |
| `POST /autoresearch/campaigns/{campaign_id}/decide-latest` | `POST /decisions/batch?campaign_id={campaign_id}` | 🔄 Deprecate |
| `POST /autoresearch/campaigns/{campaign_id}/decide-ready-batch` | `POST /decisions/batch?campaign_id={campaign_id}` | 🔄 Deprecate |
| `GET /runs` | `GET /runs?limit={n}&offset={n}&sort={field}` | 🔄 Improve |
| `GET /runs/{run_id}/artifacts` | `GET /runs/{run_id}/artifacts` | ✅ Keep |
| `GET /runs/{run_id}/logs` | `GET /runs/{run_id}/logs` | ✅ Keep |

---

*Document generated: 2026-04-22*
*Audited repository: `/Users/glasslab/cluster-config/services/workflow-api`*
