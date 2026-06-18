import React, { useState } from 'react'
import ModuleManager from './components/ModuleManager'
import ModuleFrame from './components/ModuleFrame'

export default function App() {
  const [activeModule, setActiveModule] = useState(null)

  return (
    <div className="app">
      {activeModule ? (
        <ModuleFrame module={activeModule} onBack={() => setActiveModule(null)} />
      ) : (
        <ModuleManager onOpen={setActiveModule} />
      )}
    </div>
  )
}
