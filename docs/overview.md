# Graph-RAG Codebase Overview

This document provides a structured overview of all modules and files in the Graph-RAG project.

---

## Table of Contents

1. [Root Scripts](#1-root-scripts)
   - 1.1 [main.py](#11-mainpy)
   - 1.2 [run_creator.py](#12-run_creatorpy)
   - 1.3 [run_validator.py](#13-run_validatorpy)
   - 1.4 [start_orchestrator.py](#14-start_orchestratorpy)
   - 1.5 [job_status.py](#15-job_statuspy)
   - 1.6 [list_jobs.py](#16-list_jobspy)
   - 1.7 [cancel_job.py](#17-cancel_jobpy)
   - 1.8 [validate_metamodel.py](#18-validate_metamodelpy)
   - 1.9 [app_init.py](#19-app_initpy)
   - 1.10 [db_init.py](#110-db_initpy)
   - 1.11 [neo4j_init.py](#111-neo4j_initpy)

2. [Source Package (src/)](#2-source-package-src)
   - 2.1 [src/__init__.py](#21-src__init__py)
   - 2.2 [src/workflow.py](#22-srcworkflowpy)
   - 2.3 [src/chain_example.py](#23-srcchain_examplepy)

3. [Core Module (src/core/)](#3-core-module-srccore)
   - 3.1 [src/core/__init__.py](#31-srccore__init__py)
   - 3.2 [src/core/config.py](#32-srccoreconfigpy)
   - 3.3 [src/core/neo4j_utils.py](#33-srccoreneo4j_utilspy)
   - 3.4 [src/core/postgres_utils.py](#34-srccorepostgres_utilspy)
   - 3.5 [src/core/metamodel_validator.py](#35-srccoremetamodel_validatorpy)
   - 3.6 [src/core/citation_utils.py](#36-srccorecitation_utilspy)
   - 3.7 [src/core/document_processor.py](#37-srccoredocument_processorpy)
   - 3.8 [src/core/document_models.py](#38-srccoredocument_modelspy)
   - 3.9 [src/core/csv_processor.py](#39-srccorecsv_processorpy)

4. [Agents Package (src/agents/)](#4-agents-package-srcagents)
   - 4.1 [src/agents/__init__.py](#41-srcagents__init__py)
   - 4.2 [Legacy Agents](#42-legacy-agents)
     - 4.2.1 [src/agents/graph_agent.py](#421-srcagentsgraph_agentpy)
     - 4.2.2 [src/agents/document_processor_agent.py](#422-srcagentsdocument_processor_agentpy)
     - 4.2.3 [src/agents/requirement_extractor_agent.py](#423-srcagentsrequirement_extractor_agentpy)
     - 4.2.4 [src/agents/requirement_validator_agent.py](#424-srcagentsrequirement_validator_agentpy)
     - 4.2.5 [src/agents/document_ingestion_supervisor.py](#425-srcagentsdocument_ingestion_supervisorpy)

5. [Creator Agent (src/agents/creator/)](#5-creator-agent-srcagentscreator)
   - 5.1 [src/agents/creator/__init__.py](#51-srcagentscreator__init__py)
   - 5.2 [src/agents/creator/creator_agent.py](#52-srcagentscreatorcreator_agentpy)
   - 5.3 [src/agents/creator/app.py](#53-srcagentscreatorapppy)
   - 5.4 [src/agents/creator/models.py](#54-srcagentscreatormodelspy)
   - 5.5 [src/agents/creator/document_processor.py](#55-srcagentscreatordocument_processorpy)
   - 5.6 [src/agents/creator/candidate_extractor.py](#56-srcagentscreatorcandidate_extractorpy)
   - 5.7 [src/agents/creator/researcher.py](#57-srcagentscreatorresearcherpy)
   - 5.8 [src/agents/creator/cache_writer.py](#58-srcagentscreatorcache_writerpy)
   - 5.9 [src/agents/creator/tools.py](#59-srcagentscreatortoolspy)

6. [Validator Agent (src/agents/validator/)](#6-validator-agent-srcagentsvalidator)
   - 6.1 [src/agents/validator/__init__.py](#61-srcagentsvalidator__init__py)
   - 6.2 [src/agents/validator/validator_agent.py](#62-srcagentsvalidatorvalidator_agentpy)
   - 6.3 [src/agents/validator/app.py](#63-srcagentsvalidatorapppy)
   - 6.4 [src/agents/validator/models.py](#64-srcagentsvalidatormodelspy)
   - 6.5 [src/agents/validator/relevance_analyzer.py](#65-srcagentsvalidatorrelevance_analyzerpy)
   - 6.6 [src/agents/validator/fulfillment_checker.py](#66-srcagentsvalidatorfulfillment_checkerpy)
   - 6.7 [src/agents/validator/graph_integrator.py](#67-srcagentsvalidatorgraph_integratorpy)
   - 6.8 [src/agents/validator/cache_reader.py](#68-srcagentsvalidatorcache_readerpy)
   - 6.9 [src/agents/validator/tools.py](#69-srcagentsvalidatortoolspy)

7. [Shared Agent Utilities (src/agents/shared/)](#7-shared-agent-utilities-srcagentsshared)
   - 7.1 [src/agents/shared/__init__.py](#71-srcagentsshared__init__py)
   - 7.2 [src/agents/shared/context_manager.py](#72-srcagentssharedcontext_managerpy)
   - 7.3 [src/agents/shared/checkpoint.py](#73-srcagentssharedcheckpointpy)
   - 7.4 [src/agents/shared/workspace.py](#74-srcagentssharedworkspacepy)
   - 7.5 [src/agents/shared/todo_manager.py](#75-srcagentssharedtodo_managerpy)

8. [Orchestrator (src/orchestrator/)](#8-orchestrator-srcorchestrator)
   - 8.1 [src/orchestrator/__init__.py](#81-srcorchestrator__init__py)
   - 8.2 [src/orchestrator/app.py](#82-srcorchestratorapppy)
   - 8.3 [src/orchestrator/job_manager.py](#83-srcorchestratorjob_managerpy)
   - 8.4 [src/orchestrator/monitor.py](#84-srcorchestratormonitorpy)
   - 8.5 [src/orchestrator/reporter.py](#85-srcorchestratorreporterpy)

9. [UI Module (src/ui/)](#9-ui-module-srcui)
   - 9.1 [src/ui/__init__.py](#91-srcui__init__py)
   - 9.2 [src/ui/home.py](#92-srcuihomepy)
   - 9.3 [src/ui/chain.py](#93-srcuichainpy)
   - 9.4 [src/ui/legacy_agent.py](#94-srcuilegacy_agentpy)
   - 9.5 [src/ui/document_ingestion.py](#95-srcuidocument_ingestionpy)
   - 9.6 [src/ui/agent_client.py](#96-srcuiagent_clientpy)
   - 9.7 [src/ui/creator_agent.py](#97-srcuicreator_agentpy)
   - 9.8 [src/ui/validator_agent.py](#98-srcuivalidator_agentpy)

10. [Scripts (scripts/)](#10-scripts-scripts)
    - 10.1 [scripts/app_init.py](#101-scriptsapp_initpy)
    - 10.2 [scripts/init_db.py](#102-scriptsinit_dbpy)
    - 10.3 [scripts/init_neo4j.py](#103-scriptsinit_neo4jpy)
    - 10.4 [scripts/init_mongodb.py](#104-scriptsinit_mongodbpy)

11. [Configuration (config/)](#11-configuration-config)
    - 11.1 [config/llm_config.json](#111-configllm_configjson)
    - 11.2 [config/prompts/](#112-configprompts)

12. [Docker Configuration (docker/)](#12-docker-configuration-docker)
    - 12.1 [docker/Dockerfile.base](#121-dockerdockerfilebase)
    - 12.2 [docker/Dockerfile.creator](#122-dockerdockerfilecreator)
    - 12.3 [docker/Dockerfile.validator](#123-dockerdockerfilevalidator)
    - 12.4 [docker/Dockerfile.orchestrator](#124-dockerdockerfileorchestrator)
    - 12.5 [docker/Dockerfile.dashboard](#125-dockerdockerfiledashboard)
    - 12.6 [docker/init.sql](#126-dockerinitsql)
    - 12.7 [docker-compose.yml](#127-docker-composeyml)
    - 12.8 [docker-compose.dbs.yml](#128-docker-composedbsyml)
    - 12.9 [docker-compose.dev.yml](#129-docker-composedevyml)

---

## 1. Root Scripts

### 1.1 main.py
<!-- TODO: Add description -->

### 1.2 run_creator.py
<!-- TODO: Add description -->

### 1.3 run_validator.py
<!-- TODO: Add description -->

### 1.4 start_orchestrator.py
<!-- TODO: Add description -->

### 1.5 job_status.py
<!-- TODO: Add description -->

### 1.6 list_jobs.py
<!-- TODO: Add description -->

### 1.7 cancel_job.py
<!-- TODO: Add description -->

### 1.8 validate_metamodel.py
<!-- TODO: Add description -->

### 1.9 app_init.py
<!-- TODO: Add description -->

### 1.10 db_init.py
<!-- TODO: Add description -->

### 1.11 neo4j_init.py
<!-- TODO: Add description -->

---

## 2. Source Package (src/)

### 2.1 src/__init__.py
<!-- TODO: Add description -->

### 2.2 src/workflow.py
<!-- TODO: Add description -->

### 2.3 src/chain_example.py
<!-- TODO: Add description -->

---

## 3. Core Module (src/core/)

### 3.1 src/core/__init__.py
<!-- TODO: Add description -->

### 3.2 src/core/config.py
<!-- TODO: Add description -->

### 3.3 src/core/neo4j_utils.py
<!-- TODO: Add description -->

### 3.4 src/core/postgres_utils.py
<!-- TODO: Add description -->

### 3.5 src/core/metamodel_validator.py
<!-- TODO: Add description -->

### 3.6 src/core/citation_utils.py
<!-- TODO: Add description -->

### 3.7 src/core/document_processor.py
<!-- TODO: Add description -->

### 3.8 src/core/document_models.py
<!-- TODO: Add description -->

### 3.9 src/core/csv_processor.py
<!-- TODO: Add description -->

---

## 4. Agents Package (src/agents/)

### 4.1 src/agents/__init__.py
<!-- TODO: Add description -->

### 4.2 Legacy Agents

#### 4.2.1 src/agents/graph_agent.py
<!-- TODO: Add description -->

#### 4.2.2 src/agents/document_processor_agent.py
<!-- TODO: Add description -->

#### 4.2.3 src/agents/requirement_extractor_agent.py
<!-- TODO: Add description -->

#### 4.2.4 src/agents/requirement_validator_agent.py
<!-- TODO: Add description -->

#### 4.2.5 src/agents/document_ingestion_supervisor.py
<!-- TODO: Add description -->

---

## 5. Creator Agent (src/agents/creator/)

### 5.1 src/agents/creator/__init__.py
<!-- TODO: Add description -->

### 5.2 src/agents/creator/creator_agent.py
<!-- TODO: Add description -->

### 5.3 src/agents/creator/app.py
<!-- TODO: Add description -->

### 5.4 src/agents/creator/models.py
<!-- TODO: Add description -->

### 5.5 src/agents/creator/document_processor.py
<!-- TODO: Add description -->

### 5.6 src/agents/creator/candidate_extractor.py
<!-- TODO: Add description -->

### 5.7 src/agents/creator/researcher.py
<!-- TODO: Add description -->

### 5.8 src/agents/creator/cache_writer.py
<!-- TODO: Add description -->

### 5.9 src/agents/creator/tools.py
<!-- TODO: Add description -->

---

## 6. Validator Agent (src/agents/validator/)

### 6.1 src/agents/validator/__init__.py
<!-- TODO: Add description -->

### 6.2 src/agents/validator/validator_agent.py
<!-- TODO: Add description -->

### 6.3 src/agents/validator/app.py
<!-- TODO: Add description -->

### 6.4 src/agents/validator/models.py
<!-- TODO: Add description -->

### 6.5 src/agents/validator/relevance_analyzer.py
<!-- TODO: Add description -->

### 6.6 src/agents/validator/fulfillment_checker.py
<!-- TODO: Add description -->

### 6.7 src/agents/validator/graph_integrator.py
<!-- TODO: Add description -->

### 6.8 src/agents/validator/cache_reader.py
<!-- TODO: Add description -->

### 6.9 src/agents/validator/tools.py
<!-- TODO: Add description -->

---

## 7. Shared Agent Utilities (src/agents/shared/)

### 7.1 src/agents/shared/__init__.py
<!-- TODO: Add description -->

### 7.2 src/agents/shared/context_manager.py
<!-- TODO: Add description -->

### 7.3 src/agents/shared/checkpoint.py
<!-- TODO: Add description -->

### 7.4 src/agents/shared/workspace.py
<!-- TODO: Add description -->

### 7.5 src/agents/shared/todo_manager.py
<!-- TODO: Add description -->

---

## 8. Orchestrator (src/orchestrator/)

### 8.1 src/orchestrator/__init__.py
<!-- TODO: Add description -->

### 8.2 src/orchestrator/app.py
<!-- TODO: Add description -->

### 8.3 src/orchestrator/job_manager.py
<!-- TODO: Add description -->

### 8.4 src/orchestrator/monitor.py
<!-- TODO: Add description -->

### 8.5 src/orchestrator/reporter.py
<!-- TODO: Add description -->

---

## 9. UI Module (src/ui/)

### 9.1 src/ui/__init__.py
<!-- TODO: Add description -->

### 9.2 src/ui/home.py
<!-- TODO: Add description -->

### 9.3 src/ui/chain.py
<!-- TODO: Add description -->

### 9.4 src/ui/legacy_agent.py
<!-- TODO: Add description -->

### 9.5 src/ui/document_ingestion.py
<!-- TODO: Add description -->

### 9.6 src/ui/agent_client.py
<!-- TODO: Add description -->

### 9.7 src/ui/creator_agent.py
<!-- TODO: Add description -->

### 9.8 src/ui/validator_agent.py
<!-- TODO: Add description -->

---

## 10. Scripts (scripts/)

### 10.1 scripts/app_init.py
<!-- TODO: Add description -->

### 10.2 scripts/init_db.py
<!-- TODO: Add description -->

### 10.3 scripts/init_neo4j.py
<!-- TODO: Add description -->

### 10.4 scripts/init_mongodb.py
<!-- TODO: Add description -->

---

## 11. Configuration (config/)

### 11.1 config/llm_config.json
<!-- TODO: Add description -->

### 11.2 config/prompts/
Contains system prompts for all agents and workflows (13 prompt files for legacy agents, Creator Agent phases, and Validator Agent phases).

---

## 12. Docker Configuration (docker/)

### 12.1 docker/Dockerfile.base
<!-- TODO: Add description -->

### 12.2 docker/Dockerfile.creator
<!-- TODO: Add description -->

### 12.3 docker/Dockerfile.validator
<!-- TODO: Add description -->

### 12.4 docker/Dockerfile.orchestrator
<!-- TODO: Add description -->

### 12.5 docker/Dockerfile.dashboard
<!-- TODO: Add description -->

### 12.6 docker/init.sql
<!-- TODO: Add description -->

### 12.7 docker-compose.yml
<!-- TODO: Add description -->

### 12.8 docker-compose.dbs.yml
<!-- TODO: Add description -->

### 12.9 docker-compose.dev.yml
<!-- TODO: Add description -->

---

## Summary Statistics

| Category | Count |
|----------|-------|
| Root Scripts | 11 |
| Source Package (src/) | 3 |
| Core Module | 9 |
| Agents Package + Legacy | 6 |
| Creator Agent | 9 |
| Validator Agent | 9 |
| Shared Utilities | 5 |
| Orchestrator | 5 |
| UI Module | 8 |
| Scripts | 4 |
| Configuration | 2 |
| Docker Files | 9 |
| **Total** | **80** |
