# Next Steps Feature - Implementation Summary

## Overview
This feature automatically suggests the next tasks to work on after completing or approving/rejecting a task. It provides a seamless workflow where users can immediately select and start new tasks without navigating away.

## Implementation

### Database Schema
- **New Table**: `TaskSuggestion`
  - `id`: Primary key
  - `task_id`: Foreign key to the completed task
  - `suggested_task_ids`: JSON array of task IDs that were suggested
  - `selected_task_ids`: JSON array of task IDs the user selected
  - `action_taken`: User's action ('selected' | 'skipped' | 'created_new')
  - `created_at`: Timestamp

- **Migration**: `002_add_tasksuggestion_table.py`
  - Idempotent migration that creates the table if it doesn't exist
  - Follows the same pattern as migration 001

### Backend API

#### New Endpoints
1. **GET `/tasks/{task_id}/next-steps`**
   - Returns pending tasks in the project
   - Returns sibling tasks if the task is a subtask
   - Returns parent task information
   - Response structure:
     ```json
     {
       "task": {...},
       "parent": {...} | null,
       "pending_tasks": [...],
       "sibling_tasks": [...]
     }
     ```

2. **POST `/tasks/{task_id}/next-steps`**
   - Saves the user's choice for next steps
   - Body:
     ```json
     {
       "suggested_task_ids": [1, 2, 3],
       "selected_task_ids": [1],
       "action": "selected"
     }
     ```
   - Returns the created `TaskSuggestion` record

#### Orchestrator Methods
- `get_next_steps(task_id)`: Queries and returns suggested next tasks
- `save_next_steps_choice(task_id, suggested_ids, selected_ids, action)`: Records user's choice

### Frontend Components

#### NextStepsModal Component
Location: `frontend/components/NextStepsModal.tsx`

Features:
- Displays pending tasks grouped by:
  - Related subtasks (siblings)
  - Other pending tasks in the project
- Multi-select interface with checkboxes
- Shows task status badges
- Provides "Skip" and "Start Task(s)" actions
- Links back to parent task if applicable
- Automatically starts selected tasks when confirmed

#### Integration Points
Location: `frontend/app/tasks/[id]/page.tsx`

The modal is triggered in three scenarios:

1. **After Task Completion** (line ~127):
   ```typescript
   es.addEventListener("done", async () => {
     const updatedTask = await load();
     if (updatedTask?.status === "awaiting_approval") {
       setShowNextStepsModal(true);
     }
   });
   ```

2. **After Task Approval** (line ~240):
   ```typescript
   async function onApprove() {
     const res = await api.approve(taskId);
     if (res.ok) {
       setShowNextStepsModal(true);
     }
   }
   ```

3. **After Task Rejection** (line ~260):
   ```typescript
   async function onReject() {
     await api.reject(taskId);
     setShowNextStepsModal(true);
   }
   ```

#### Task Selection Handler
```typescript
async function handleTasksSelected(taskIds: number[]) {
  // Start all selected tasks
  for (const tid of taskIds) {
    await api.runTask(tid);
  }
  // Navigate to first task if only one selected
  if (taskIds.length === 1) {
    window.location.href = `/tasks/${taskIds[0]}`;
  }
}
```

### API Client Updates
Location: `frontend/lib/api.ts`

New types:
- `NextStepsResponse`
- `TaskSuggestion`

New API methods:
- `getNextSteps(taskId)`: Fetch next step suggestions
- `saveNextStepsChoice(taskId, suggestedIds, selectedIds, action)`: Save user's choice

## User Experience Flow

1. User runs a task → Task completes → Status becomes "awaiting_approval"
2. Modal automatically appears showing:
   - Related subtasks (if any)
   - Other pending tasks in the project
3. User can:
   - Select one or multiple tasks to start
   - Skip and stay on current task page
   - Close modal to review current task
4. If user selects tasks:
   - Tasks are automatically started
   - If single task: navigates to that task
   - If multiple tasks: stays on current page (tasks run in background)
5. User's choice is recorded in the database for analytics

## Benefits

1. **Seamless Workflow**: No need to navigate back to project page between tasks
2. **Context Awareness**: Prioritizes related subtasks and pending work
3. **Flexibility**: Supports both single and parallel task execution
4. **Analytics**: Tracks user behavior for future workflow improvements
5. **Non-Intrusive**: Can be skipped or closed without affecting current work

## Testing

### Backend Tests
```bash
cd backend
# Test database initialization
python -c "from app.storage.db import init_db; init_db()"

# Verify table structure
python -c "from app.storage.db import engine; from sqlalchemy import inspect; print(inspect(engine).get_table_names())"
```

### Manual Testing Checklist
- [ ] Create a project with multiple tasks
- [ ] Run a task and wait for completion
- [ ] Verify modal appears with pending tasks
- [ ] Select single task → verify it starts and navigation occurs
- [ ] Select multiple tasks → verify all start
- [ ] Click "Skip" → verify modal closes and stays on page
- [ ] Test after approving a task
- [ ] Test after rejecting a task
- [ ] Verify database records are created in `tasksuggestion` table

## Future Enhancements

1. **Smart Prioritization**: Use ML to suggest most relevant tasks based on:
   - Previous user choices
   - Task dependencies
   - Project context

2. **Quick Actions**:
   - "Start all subtasks" button
   - "Create new related task" option

3. **Keyboard Shortcuts**:
   - Number keys to select tasks
   - Enter to confirm
   - Escape to close

4. **Preview Mode**:
   - Show task descriptions on hover
   - Preview estimated complexity

5. **Parallel Execution Limits**:
   - Warn if too many tasks selected
   - Show resource usage estimates
