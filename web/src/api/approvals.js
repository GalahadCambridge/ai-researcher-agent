import { http } from './client'

export const listApprovals = (pid, params) => http.get(`/projects/${pid}/approvals`, { quiet: true, params })
export const getApproval = (pid, aid) => http.get(`/projects/${pid}/approvals/${aid}`, { quiet: true })
export const decide = (pid, aid, body) => http.post(`/projects/${pid}/approvals/${aid}/decision`, body)
