import { http } from './client'

const q = { quiet: true }
export const listRuns = (pid, params) => http.get(`/projects/${pid}/runs`, { ...q, params })
export const getRun = (pid, rid) => http.get(`/projects/${pid}/runs/${rid}`, q)
export const getLogs = (pid, rid, stream = 'stdout', offset = 0) =>
  http.get(`/projects/${pid}/runs/${rid}/logs`, { ...q, params: { stream, offset } })
export const cancelRun = (pid, rid, requestId) => http.post(`/projects/${pid}/runs/${rid}/cancel`, { request_id: requestId })
export const labelRun = (pid, rid, body) => http.post(`/projects/${pid}/runs/${rid}/label`, body)
