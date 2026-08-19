"""Task 관리 화면.

화면은 토큰을 저장하지 않고 현재 브라우저 요청에만 Authorization 헤더로
전달한다. 실제 데이터와 권한 검증은 `/api/v1/tasks` API가 담당한다.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(tags=["task-ui"])

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workmate Task 관리</title>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#172033}
label{display:block;margin:.75rem 0 .25rem}input,select,button{box-sizing:border-box;padding:.55rem;font:inherit}
input,select{width:100%;border:1px solid #b8c0cc;border-radius:4px}button{border:1px solid #8290a5;border-radius:4px;background:#f6f8fb;cursor:pointer}
button.primary{background:#185adb;color:white;border-color:#185adb}.toolbar{display:flex;gap:.5rem;margin-top:1rem}.toolbar button{flex:1}
table{width:100%;border-collapse:collapse;margin-top:1.5rem}th,td{text-align:left;border-bottom:1px solid #d9dee8;padding:.6rem;vertical-align:top}
.muted{color:#5c687a}pre{white-space:pre-wrap;background:#f4f6f9;padding:1rem;min-height:2rem;border-radius:4px}
</style></head><body>
<h1>Workmate Task 관리</h1>
<p class="muted">입력한 Bearer Token은 저장하지 않고 현재 요청에만 사용합니다.</p>
<label for="token">OIDC Bearer Token</label><input id="token" type="password" autocomplete="off">
<input id="taskId" type="hidden">
<label for="title">제목</label><input id="title" maxlength="500" placeholder="예: 주간 보고서 작성">
<label for="status">상태</label><select id="status"><option value="todo">todo</option><option value="in_progress">in_progress</option><option value="blocked">blocked</option><option value="done">done</option><option value="cancelled">cancelled</option></select>
<label for="priority">우선순위 힌트 (0-10)</label><input id="priority" type="number" min="0" max="10">
<label for="dueAt">마감일</label><input id="dueAt" type="datetime-local">
<div class="toolbar"><button id="save" class="primary" type="button">등록</button><button id="reset" type="button">입력 초기화</button><button id="refresh" type="button">새로고침</button></div>
<p id="summary" aria-live="polite">Task를 불러오지 않았습니다.</p><pre id="error"></pre>
<table><thead><tr><th>제목</th><th>상태</th><th>마감일</th><th>동작</th></tr></thead><tbody id="tasks"></tbody></table>
<script>
const token=document.querySelector('#token'),taskId=document.querySelector('#taskId'),title=document.querySelector('#title'),status=document.querySelector('#status'),priority=document.querySelector('#priority'),dueAt=document.querySelector('#dueAt'),save=document.querySelector('#save'),reset=document.querySelector('#reset'),refresh=document.querySelector('#refresh'),summary=document.querySelector('#summary'),error=document.querySelector('#error'),rows=document.querySelector('#tasks');
const headers=()=>({'Authorization':`Bearer ${token.value}`,'Content-Type':'application/json'});
const body=()=>({title:title.value.trim(),status:status.value,priority_hint:priority.value===''?null:Number(priority.value),due_at:dueAt.value?new Date(dueAt.value).toISOString():null});
function clearForm(){taskId.value='';title.value='';status.value='todo';priority.value='';dueAt.value='';save.textContent='등록'}
function edit(task){taskId.value=task.task_id;title.value=task.title;status.value=task.status;priority.value=task.priority_hint??'';dueAt.value=task.due_at?task.due_at.slice(0,16):'';save.textContent='수정';title.focus()}
function render(items){rows.replaceChildren(...items.map(task=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${escapeHtml(task.title)}</td><td>${task.status}</td><td>${task.due_at||'-'}</td><td><button data-edit="${task.task_id}">수정</button> <button data-delete="${task.task_id}">삭제</button></td>`;return tr}));rows.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>loadOne(b.dataset.edit));rows.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>removeTask(b.dataset.delete));summary.textContent=`Task ${items.length}건`}
function escapeHtml(value){return String(value).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
async function request(path,options){const response=await fetch(path,{...options,headers:{...headers(),...(options?.headers||{})}});const raw=await response.text();let data;try{data=raw?JSON.parse(raw):null}catch{data=raw}if(!response.ok)throw new Error(`HTTP ${response.status}: ${typeof data==='string'?data:JSON.stringify(data)}`);return data}
async function load(){error.textContent='';try{render(await request('/api/v1/tasks'))}catch(e){error.textContent=e.message}}
async function loadOne(id){try{edit(await request(`/api/v1/tasks/${id}`))}catch(e){error.textContent=e.message}}
async function removeTask(id){if(!confirm('이 Task를 삭제할까요?'))return;try{await request(`/api/v1/tasks/${id}`,{method:'DELETE'});clearForm();await load()}catch(e){error.textContent=e.message}}
save.onclick=async()=>{error.textContent='';if(!title.value.trim()){error.textContent='제목을 입력하세요.';return}try{const id=taskId.value;await request(id?`/api/v1/tasks/${id}`:'/api/v1/tasks',{method:id?'PATCH':'POST',body:JSON.stringify(body())});clearForm();await load()}catch(e){error.textContent=e.message}}
reset.onclick=clearForm;refresh.onclick=load;load();
</script></body></html>"""


@router.get("/tasks", response_class=HTMLResponse)
def task_page() -> HTMLResponse:
    """Task 목록·등록·수정·삭제 화면을 반환한다."""

    return HTMLResponse(_PAGE)


__all__ = ["router"]
