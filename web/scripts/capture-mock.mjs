// 把后台真实返回值存成假数据（详细设计 5 第 5.2 节）。先启动 air serve 并登记样例项目，然后：
//   npm run mock:capture              # 用后台的第一个项目
//   npm run mock:capture -- <pid>     # 指定项目
import { mkdir, writeFile } from 'node:fs/promises'

const BASE = (process.env.AIR_SERVER || 'http://127.0.0.1:8765') + '/api'
const OUT = new URL('../src/mock/data/', import.meta.url)

async function get(path) {
  const res = await fetch(BASE + path)
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  return res.json()
}
async function save(name, data) {
  await writeFile(new URL(`${name}.json`, OUT), JSON.stringify(data, null, 2) + '\n', 'utf8')
  console.log('✓', name)
}

await mkdir(OUT, { recursive: true })
const projects = await get('/projects')
const pid = process.argv[2] || projects[0]?.project_id
if (!pid) throw new Error('后台没有项目：先运行 air dev new-workspace <目录> --from fixtures/sample_project --register')
const P = `/projects/${pid}`

await save('tasks', await get('/tasks'))
await save('defaults', await get('/defaults'))
await save('projects', projects)
await save('project_status', await get(P))
const approvals = await get(`${P}/approvals`)
await save('approvals', approvals)
for (const a of approvals) await save(`approval_${a.approval_id}`, await get(`${P}/approvals/${a.approval_id}`))
const runs = await get(`${P}/runs`)
await save('runs', runs)
const run = runs.find((r) => r.state === 'failed') || runs[0]
if (run) {
  await save('run_detail', await get(`${P}/runs/${run.run_id}`))
  await save('run_logs', await get(`${P}/runs/${run.run_id}/logs`))
}
const lit = await get(`${P}/literature`)
await save('literature', lit)
if (lit[0]) await save('literature_record', await get(`${P}/literature/${encodeURIComponent(lit[0].paper_id)}`))
await save('figures', await get(`${P}/figures`))
await save('paper', await get(`${P}/paper`))
const claims = await get(`${P}/claims`)
await save('claims', claims)
if (claims.claims[0]) await save('claim_trace', await get(`${P}/claims/${claims.claims[0].claim_id}/trace`))
await save('templates', await get(`${P}/templates`))
await save('events', await get(`${P}/events?limit=100000`))
await save('checkpoints', await get(`${P}/checkpoints`))
console.log(`完成：项目 ${pid}`)
