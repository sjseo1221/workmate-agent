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
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}label{display:block;margin:.8rem 0 .25rem}input,select,textarea,button{box-sizing:border-box;width:100%;padding:.6rem;font:inherit}textarea{min-height:9rem;font-family:monospace}button{margin-top:1rem;cursor:pointer}pre{white-space:pre-wrap;background:#f4f4f4;padding:1rem;min-height:4rem}</style>
</head>
<body><h1>Workmate Skill 검증</h1>
<p>입력한 Bearer Token은 저장하지 않고 현재 브라우저 요청에만 사용합니다.</p>
<label for="token">OIDC Bearer Token</label><input id="token" type="password" autocomplete="off">
<label for="skill">Skill</label><select id="skill"><option value="">Token 입력 후 Skill 목록을 불러오세요</option></select>
<label for="mode">입력 형식</label><select id="mode"><option value="text">자연어</option><option value="json">JSON</option></select>
<label for="input">입력</label><textarea id="input" placeholder="질문 또는 승인된 Skill JSON"></textarea>
<button id="send" type="button">실행</button><section aria-live="polite"><h2>처리 결과</h2><p id="summary">아직 실행하지 않았습니다.</p><p id="warnings"></p><pre id="result"></pre></section>
<script>
const token=document.querySelector('#token'), skill=document.querySelector('#skill'), mode=document.querySelector('#mode'), input=document.querySelector('#input'), result=document.querySelector('#result'), summary=document.querySelector('#summary'), warnings=document.querySelector('#warnings');
const headers=()=>({Authorization:`Bearer ${token.value}`,'Content-Type':'application/json'});
async function loadSkills(){if(!token.value.trim()){skill.replaceChildren(new Option('Token 입력 후 Skill 목록을 불러오세요',''));return false} const r=await fetch('/api/v1/internal/skill-chat/skills',{headers:headers()}); if(!r.ok){skill.replaceChildren(new Option(`Skill 목록 오류: HTTP ${r.status}`,''));result.textContent=`Skill 목록 오류: HTTP ${r.status}`;return false} skill.replaceChildren(...(await r.json()).map(s=>new Option(`${s.id} — ${s.name}`,s.id)));return true;}
token.addEventListener('input',()=>{loadSkills()});
document.querySelector('#send').onclick=async()=>{if(!skill.value&&!await loadSkills()){return} let value=input.value; if(mode.value==='json'){try{value=JSON.parse(value)}catch(e){summary.textContent='입력 오류';warnings.textContent='';result.textContent='JSON 형식이 올바르지 않습니다.';return}} if(typeof value==='string'&&!value.trim()){summary.textContent='입력 오류';warnings.textContent='';result.textContent='입력을 입력해 주세요.';return} const r=await fetch('/api/v1/internal/skill-chat/messages',{method:'POST',headers:headers(),body:JSON.stringify({skill_id:skill.value,input:value})}); const raw=await r.text(); result.textContent=raw; try{const data=JSON.parse(raw); if(r.ok){const a=data.artifact||{}; summary.textContent=`Skill: ${data.skill_id} · 상태: ${data.state} · Task: ${data.task_id} · Mock: ${a.mock===true?'예':'아니오'} · 실제 업무 결과: ${a.business_result===true?'예':'아니오'}`; warnings.textContent=(data.warnings||[]).length?`경고: ${(data.warnings||[]).map(w=>`${w.source}: ${w.message}`).join(' / ')}`:'경고 없음'}else{summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}}catch(e){summary.textContent=`오류: HTTP ${r.status}`;warnings.textContent=''}};
loadSkills();
</script></body></html>"""


@router.get("/internal/skill-chat", response_class=HTMLResponse)
def internal_chat_page() -> HTMLResponse:
    """내부 검증 UI를 반환하고 비활성 환경에서는 진입을 차단한다."""

    if os.getenv(INTERNAL_CHAT_ENABLED_ENV, "").lower() != "true":
        raise HTTPException(status_code=404, detail="internal chat is disabled")
    return HTMLResponse(_PAGE)


__all__ = ["router"]
