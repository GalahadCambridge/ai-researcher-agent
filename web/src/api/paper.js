import { http } from './client'

const q = { quiet: true }
export const listLiterature = (pid) => http.get(`/projects/${pid}/literature`, q)
export const getPaperRecord = (pid, paperId) => http.get(`/projects/${pid}/literature/${encodeURIComponent(paperId)}`, q)
export const listFigures = (pid) => http.get(`/projects/${pid}/figures`, q)
export const getPaper = (pid) => http.get(`/projects/${pid}/paper`, q)
export const pdfUrl = (pid, version) => `/api/projects/${pid}/paper/pdf${version ? `?version=${version}` : ''}`
export const listClaims = (pid) => http.get(`/projects/${pid}/claims`, q)
export const claimTrace = (pid, claimId) => http.get(`/projects/${pid}/claims/${claimId}/trace`, q)
