"""주간 보고서 최소 화면."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(tags=["weekly-report-ui"])

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workmate 주간 보고서</title>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#172033}
label{display:block;margin:.75rem 0 .25rem}input,button{box-sizing:border-box;padding:.6rem;font:inherit;border:1px solid #b8c0cc;border-radius:4px}input{width:100%}
.toolbar{display:flex;gap:.5rem;margin-top:1rem}.toolbar button{flex:1;cursor:pointer}.primary{background:#185adb;color:#fff;border-color:#185adb}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin-top:1.5rem}.card{border:1px solid #d9dee8;border-radius:6px;padding:1rem}.card h2{font-size:1rem;margin-top:0}.muted{color:#5c687a}pre{white-space:pre-wrap;background:#f4f6f9;padding:1rem;border-radius:4px;min-height:5rem}
</style></head><body>
<h1>Workmate 주간 보고서</h1>
<p class="muted">토큰은 저장하지 않고 현재 브라우저 요청에만 사용합니다.</p>
<label for="token">OIDC Bearer Token</label><input id="token" type="password" autocomplete="off">
<label for="week">기준 주(월요일)</label><input id="week" type="date">
<div class="toolbar"><button id="generate" class="primary" type="button">보고서 생성</button><button id="copy" type="button" disabled>Markdown 복사</button></div>
<p id="status" aria-live="polite">보고서를 생성하지 않았습니다.</p><pre id="error"></pre>
<section id="report" hidden><h2 id="period"></h2><p id="summary"></p><div class="grid" id="sections"></div><h2>Markdown</h2><pre id="markdown"></pre></section>
<script>
const token=document.querySelector('#token'),week=document.querySelector('#week'),generate=document.querySelector('#generate'),copy=document.querySelector('#copy'),status=document.querySelector('#status'),error=document.querySelector('#error'),report=document.querySelector('#report'),period=document.querySelector('#period'),summary=document.querySelector('#summary'),sections=document.querySelector('#sections'),markdown=document.querySelector('#markdown');
const today=new Date(); today.setDate(today.getDate()-((today.getDay()+6)%7)); week.value=today.toISOString().slice(0,10);
let markdownValue='';
const headers=()=>({'Authorization':`Bearer ${token.value}`,'Content-Type':'application/json'});
const labels={completed:'완료',in_progress:'진행',delayed:'지연',unresolved_issues:'미해결 이슈',next_week_plans:'다음 주 계획'};
function show(data){const result=data.artifact?.data?.data; markdownValue=data.artifact?.markdown||''; if(!result)throw new Error('보고서 구조화 결과가 없습니다.'); report.hidden=false; period.textContent=`${result.period.start} ~ ${result.period.end}`; summary.textContent=result.summary||'요약 없음'; sections.replaceChildren(...Object.entries(labels).map(([key,label])=>{const card=document.createElement('article');card.className='card';const values=result[key]||[];card.innerHTML=`<h2>${label}</h2>${values.length?values.map(v=>`<p>${escapeHtml(v.title||v.text||v.summary||String(v))}</p>`).join(''):'<p class="muted">없음</p>'}`;return card})); markdown.textContent=markdownValue; copy.disabled=!markdownValue; status.textContent='보고서 생성 완료';}
function escapeHtml(value){return String(value).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
generate.onclick=async()=>{error.textContent='';status.textContent='생성 중...';generate.disabled=true;try{const response=await fetch('/api/v1/internal/skill-chat/messages',{method:'POST',headers:headers(),body:JSON.stringify({skill_id:'weekly_report',input:{schema_version:'1.0',skill_id:'weekly_report',week_of:week.value,timezone:'Asia/Seoul',locale:'ko-KR'}})});const raw=await response.text();const data=JSON.parse(raw);if(!response.ok)throw new Error(`HTTP ${response.status}: ${data.detail||raw}`);show(data)}catch(e){status.textContent='생성 실패';error.textContent=e.message}finally{generate.disabled=false}};
copy.onclick=async()=>{try{await navigator.clipboard.writeText(markdownValue);status.textContent='Markdown을 클립보드에 복사했습니다.'}catch(e){status.textContent='클립보드 복사 실패';error.textContent=e.message}};
</script></body></html>"""


@router.get("/weekly-report", response_class=HTMLResponse)
def weekly_report_page() -> HTMLResponse:
    """주간 보고서 조회와 Markdown 복사 화면을 반환한다."""

    return HTMLResponse(_PAGE)


__all__ = ["router"]
