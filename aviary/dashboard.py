"""Web 控制台页面:GET / 返回,数据由 /stats 提供。"""

DASHBOARD_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>aviary 控制台</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:ui-sans-serif,system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#0f1117;color:#e6e6eb;margin:0;padding:2rem}
 h1{font-size:1.3rem} h1 span{color:#8ab4ff}
 table{border-collapse:collapse;width:100%;margin-top:1rem;font-size:.92rem}
 th,td{border:1px solid #2a2d3a;padding:.45rem .7rem;text-align:left}
 th{background:#161926;color:#9aa3b5}
 .kpi{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}
 .kpi div{background:#161926;border:1px solid #2a2d3a;border-radius:.6rem;padding:.8rem 1.2rem}
 .kpi b{display:block;font-size:1.4rem;color:#8ab4ff}
 code{color:#9ece6a}
</style></head><body>
<h1><span>aviary</span> · 每个模型,一个入口</h1>
<div class="kpi" id="kpi"></div>
<table id="tbl"><thead><tr><th>请求模型</th><th>次数</th><th>输入 tok</th>
<th>输出 tok</th><th>成本 $</th></tr></thead><tbody></tbody></table>
<p style="color:#9aa3b5;font-size:.85rem">端点:<code>/v1/chat/completions</code> ·
<code>/v1/messages</code> · <code>/v1/responses</code> ·
<code>/v1beta/models/&lt;model&gt;:generateContent</code></p>
<script>
fetch('/stats').then(r=>r.json()).then(s=>{
 const k=document.getElementById('kpi');
 k.innerHTML=`<div>总请求<b>${s.total_requests}</b></div>
  <div>成功<b>${s.ok}</b></div><div>输入<b>${s.prompt_tokens}</b></div>
  <div>输出<b>${s.completion_tokens}</b></div><div>成本<b>$${s.cost_usd.toFixed(6)}</b></div>`;
 const tb=document.querySelector('#tbl tbody');
 tb.innerHTML=Object.entries(s.by_model).map(([m,v])=>
  `<tr><td><code>${m}</code></td><td>${v.requests}</td><td>${v.prompt_tokens}</td>
   <td>${v.completion_tokens}</td><td>${v.cost_usd.toFixed(6)}</td></tr>`).join('');
});
</script></body></html>"""
