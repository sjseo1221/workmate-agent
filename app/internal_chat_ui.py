"""내부 검증 챗봇의 최소 입력 화면."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.internal_chat import INTERNAL_CHAT_ENABLED_ENV
import os


router = APIRouter(tags=["internal-skill-chat-ui"])

_PAGE = """<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workmate Skill 검증</title>
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}label{display:block;margin:.8rem 0 .25rem}input,select,textarea,button{box-sizing:border-box;width:100%;padding:.6rem;font:inherit}textarea{min-height:9rem;font-family:monospace}button{margin-top:1rem;cursor:pointer}.actions{display:flex;gap:.5rem}.actions button{flex:1}button:disabled{cursor:not-allowed;opacity:.55}pre{white-space:pre-wrap;background:#f4f4f4;padding:1rem;min-height:4rem}</style>
</head>
<body><h1>Workmate Skill 검증</h1>
<p>입력한 Bearer Token은 저장하지 않고 현재 브라우저 요청에만 사용합니다.</p>
<label for="token">OIDC Bearer Token</label><input id="token" type="password" autocomplete="off">
<label for="skill">Skill</label><select id="skill"><option value="">Token 입력 후 Skill 목록을 불러오세요</option></select>
<label for="mode">입력 형식</label><select id="mode"><option value="text">자연어</option><option value="json">JSON</option></select>
<label for="input">입력</label><textarea id="input" placeholder="질문 또는 승인된 Skill JSON"></textarea>
<button id="send" type="button">실행</button><div class="actions"><button id="poll" type="button" disabled>상태 새로고침</button><button id="cancel" type="button" disabled>Task 취소</button></div><section aria-live="polite"><h2>처리 결과</h2><p id="summary">아직 실행하지 않았습니다.</p><p id="warnings"></p><pre id="result"></pre></section>
<script>
const token=document.querySelector('#token'), skill=document.querySelector('#skill'), mode=document.querySelector('#mode'), input=document.querySelector('#input'), send=document.querySelector('#send'), poll=document.querySelector('#poll'), cancel=document.querySelector('#cancel'), result=document.querySelector('#result'), summary=document.querySelector('#summary'), warnings=document.querySelector('#warnings'); let activeTaskId=null; let pollTimer=null;
const headers=()=>({Authorization:`Bearer ${token.value}`,'Content-Type':'application/json'});
const stateOf=data=>String(data.state||data.status?.state||'').toLowerCase();
const terminal=data=>['completed','failed','canceled','cancelled','rejected','task_state_completed','task_state_failed','task_state_canceled','task_state_rejected'].some(s=>stateOf(data).includes(s));
function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null}}
function showTask(data){const a=data.artifact||data.artifacts?.[0]||{}; const state=data.state||data.status?.state||'unknown'; summary.textContent=`Skill: ${data.skill_id||skill.value} · 상태: ${state} · Task: ${data.task_id||activeTaskId} · Mock: ${a.mock===true?'예':'아니오'} · 실제 업무 결과: ${a.business_result===true?'예':'아니오'}`; warnings.textContent=(data.warnings||[]).length?`경고: ${(data.warnings||[]).map(w=>`${w.source}: ${w.message}`).join(' / ')}`:'경고 없음'; result.textContent=JSON.stringify(data,null,2); const done=terminal(data); poll.disabled=!activeTaskId||done; cancel.disabled=!activeTaskId||done; if(done)stopPolling()}
async function loadSkills(){if(!token.value.trim()){skill.replaceChildren(new Option('Token 입력 후 Skill 목록을 불러오세요',''));return false} const r=await fetch('/api/v1/internal/skill-chat/skills',{headers:headers()}); if(!r.ok){skill.replaceChildren(new Option(`Skill 목록 오류: HTTP ${r.status}`,''));result.textContent=`Skill 목록 오류: HTTP ${r.status}`;return false} skill.replaceChildren(...(await r.json()).map(s=>new Option(`${s.id} — ${s.name}`,s.id)));return true;}
async function refreshTask(){if(!activeTaskId)return; const r=await fetch(`/api/v1/internal/skill-chat/tasks/${activeTaskId}`,{headers:headers()}); const raw=await r.text(); if(!r.ok){summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent='';result.textContent=raw;stopPolling();return} showTask(JSON.parse(raw));}
function startPolling(){stopPolling(); refreshTask(); pollTimer=setInterval(refreshTask,1000)}
token.addEventListener('input',()=>{loadSkills()});
send.onclick=async()=>{if(!skill.value&&!await loadSkills()){return} let value=input.value; if(mode.value==='json'){try{value=JSON.parse(value)}catch(e){summary.textContent='입력 오류';warnings.textContent='';result.textContent='JSON 형식이 올바르지 않습니다.';return}} if(typeof value==='string'&&!value.trim()){summary.textContent='입력 오류';warnings.textContent='';result.textContent='입력을 입력해 주세요.';return} const r=await fetch('/api/v1/internal/skill-chat/messages',{method:'POST',headers:headers(),body:JSON.stringify({skill_id:skill.value,input:value})}); const raw=await r.text(); result.textContent=raw; try{const data=JSON.parse(raw); if(r.ok){activeTaskId=data.task_id; showTask(data); startPolling()}else{summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}}catch(e){summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}};
poll.onclick=refreshTask;
cancel.onclick=async()=>{if(!activeTaskId)return; const r=await fetch(`/api/v1/internal/skill-chat/tasks/${activeTaskId}:cancel`,{method:'POST',headers:headers()}); const raw=await r.text(); result.textContent=raw; try{const data=JSON.parse(raw); if(r.ok)showTask(data); else{summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}}catch(e){summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}};
loadSkills();
</script></body></html>"""


@router.get("/internal/skill-chat", response_class=HTMLResponse)
def internal_chat_page() -> HTMLResponse:
    """내부 검증 UI를 반환하고 비활성 환경에서는 진입을 차단한다."""

    if os.getenv(INTERNAL_CHAT_ENABLED_ENV, "").lower() != "true":
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    return HTMLResponse(_PAGE)


__all__ = ["router"]
