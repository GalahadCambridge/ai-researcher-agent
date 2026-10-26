import { http } from './client'

const q = { quiet: true }
export const listArtifacts = (pid, kind) => http.get(`/projects/${pid}/artifacts`, { ...q, params: { kind } })
export const artifactVersions = (pid, artifactId) =>
  http.get(`/projects/${pid}/artifacts/versions`, { ...q, params: { artifact_id: artifactId } })
export const artifactContent = (pid, artifactId, version) =>
  http.get(`/projects/${pid}/artifacts/content`, { ...q, params: { artifact_id: artifactId, version } })
export const editArtifact = (pid, body) => http.put(`/projects/${pid}/artifacts/content`, body)
export const readFile = (pid, path) => http.get(`/projects/${pid}/files`, { ...q, params: { path } })
export const fileUrl = (pid, path) => `/api/projects/${pid}/files?path=${encodeURIComponent(path)}`

// 文本文件返回 {path, content}，二进制直接返回文件。图片统一转成可放进 <img> 的地址（SVG 也是文本）
export async function fileObjectUrl(pid, path) {
  const blob = await http.get(`/projects/${pid}/files`, { ...q, params: { path }, responseType: 'blob' })
  if (blob.type.includes('json')) {
    const { content } = JSON.parse(await blob.text())
    const type = path.endsWith('.svg') ? 'image/svg+xml' : 'text/plain'
    return URL.createObjectURL(new Blob([content], { type }))
  }
  return URL.createObjectURL(blob)
}
