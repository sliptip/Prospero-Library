import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/manrope'
import '@fontsource-variable/playfair-display'
import '@fontsource/ibm-plex-mono/500.css'
import './style.css'
import { App } from './App'

createRoot(document.getElementById('root')!).render(<App />)
