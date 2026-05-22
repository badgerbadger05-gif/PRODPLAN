type ExportResponse = {
  data_base64?: string
  filename?: string
  content_type?: string
  format?: string
}

export function downloadBase64File(response: ExportResponse, fallbackName: string) {
  if (!response.data_base64) {
    throw new Error('Backend вернул пустой файл')
  }

  const binary = atob(response.data_base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i)
  }

  const blob = new Blob([bytes], {
    type: response.content_type || 'application/octet-stream',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = response.filename || fallbackName
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
