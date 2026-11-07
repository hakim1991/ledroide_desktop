import React, { useRef, useEffect } from 'react'
import './ModuleFrame.css'

const ORIGIN = window.ledroide?.backendOrigin ?? `http://127.0.0.1:${window.ledroide?.backendPort ?? 8765}`

export default function ModuleFrame({ module, hidden = false, reloadKey = 0 }) {
  const iframeRef = useRef(null)
  const src = `${ORIGIN}/modules/${module.id}/frontend/`

  useEffect(() => {
    const unsubscribers = new Map()

    const post = (message) => iframeRef.current?.contentWindow?.postMessage(message, ORIGIN)

    const handleMessage = async (event) => {
      const iframe = iframeRef.current
      if (!iframe || event.source !== iframe.contentWindow || event.origin !== ORIGIN) return
      const { type, action, payload, requestId, event: evtName, timeout } = event.data ?? {}
      // The iframe's claimed moduleId is ignored: a module may only reach its own backend.
      const moduleId = module.id

      if (type === 'ledroide:call') {
        try {
          const result = await window.ledroide.call(moduleId, action, payload, { timeout: timeout ?? 0 })
          post({ type: 'ledroide:response', requestId, payload: result })
        } catch (e) {
          post({ type: 'ledroide:response', requestId, payload: { error: e.message, code: e.code ?? 'RPC_ERROR' } })
        }
      }

      if (type === 'ledroide:subscribe' && typeof evtName === 'string' && !unsubscribers.has(evtName)) {
        unsubscribers.set(evtName, window.ledroide.on(moduleId, evtName, (data) => {
          post({ type: 'ledroide:event', moduleId, action: evtName, payload: data })
        }))
      }
    }

    window.addEventListener('message', handleMessage)
    return () => {
      window.removeEventListener('message', handleMessage)
      unsubscribers.forEach((unsubscribe) => unsubscribe())
    }
  }, [module.id, reloadKey])

  return (
    <iframe
      key={reloadKey}
      ref={iframeRef}
      className="mf-iframe"
      style={hidden ? { display: 'none' } : undefined}
      src={src}
      sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"
      title={module.name}
    />
  )
}
