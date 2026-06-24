import React, { useRef, useEffect } from 'react'
import './ModuleFrame.css'

const BACKEND_PORT = window.ledroide?.backendPort ?? 8765

export default function ModuleFrame({ module }) {
  const src = `http://127.0.0.1:${BACKEND_PORT}/modules/${module.id}/frontend/`
  const iframeRef = useRef(null)

  useEffect(() => {
    const handleMessage = async (event) => {
      const iframe = iframeRef.current
      if (!iframe || event.source !== iframe.contentWindow) return
      const { type, moduleId, action, payload, requestId, event: evtName } = event.data

      if (type === 'ledroide:call') {
        try {
          const result = await window.ledroide.call(moduleId, action, payload)
          iframe.contentWindow.postMessage({ type: 'ledroide:response', requestId, payload: result }, '*')
        } catch (e) {
          iframe.contentWindow.postMessage({ type: 'ledroide:response', requestId, payload: { error: e.message } }, '*')
        }
      }

      if (type === 'ledroide:subscribe') {
        window.ledroide.on(moduleId, evtName, (data) => {
          iframe.contentWindow?.postMessage({ type: 'ledroide:event', moduleId, action: evtName, payload: data }, '*')
        })
      }
    }

    window.addEventListener('message', handleMessage)
    return () => window.removeEventListener('message', handleMessage)
  }, [module.id])

  return (
    <iframe
      ref={iframeRef}
      className="mf-iframe"
      src={src}
      sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"
      title={module.name}
    />
  )
}
