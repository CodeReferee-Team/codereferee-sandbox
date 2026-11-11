const status = document.querySelector('#status')
const items = document.querySelector('#items')

async function refresh() {
  const response = await fetch('/api/items')
  if (!response.ok) throw new Error('Read failed')
  const payload = await response.json()
  items.replaceChildren(...payload.items.map(item => {
    const row = document.createElement('li')
    row.textContent = item.label
    return row
  }))
}

document.querySelector('#form').addEventListener('submit', async event => {
  event.preventDefault()
  status.textContent = 'Saving'
  try {
    const response = await fetch('/api/items', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: document.querySelector('#item').value})
    })
    if (!response.ok) throw new Error('Write failed')
    await refresh()
    status.textContent = 'Saved'
  } catch {
    status.textContent = 'Unavailable'
  }
})

refresh().catch(() => { status.textContent = 'Unavailable' })
