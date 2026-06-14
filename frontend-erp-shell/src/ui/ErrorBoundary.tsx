import { Component, type ErrorInfo, type ReactNode } from 'react'

type ErrorBoundaryProps = {
  children: ReactNode
}

type ErrorBoundaryState = {
  error: Error | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary caught an error', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <main className="workArea">
          <div className="errorLine">Произошла ошибка: {this.state.error.message}</div>
          <button type="button" className="filterBtn" onClick={() => window.location.reload()}>
            Перезагрузить
          </button>
        </main>
      )
    }
    return this.props.children
  }
}
