import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
	children: ReactNode;
}

interface State {
	error: Error | null;
}

/**
 * Top-level error boundary — a single render throw (e.g. an undefined clip
 * field or a malformed WS payload reaching a renderer) would otherwise
 * white-screen the whole app. This catches it and offers a reload.
 */
export class ErrorBoundary extends Component<Props, State> {
	state: State = { error: null };

	static getDerivedStateFromError(error: Error): State {
		return { error };
	}

	componentDidCatch(error: Error, info: ErrorInfo): void {
		console.error("[Kinetograph] Uncaught render error:", error, info.componentStack);
	}

	private handleReload = () => {
		this.setState({ error: null });
		window.location.reload();
	};

	render() {
		if (this.state.error) {
			return (
				<div className="flex h-screen flex-col items-center justify-center gap-4 bg-[#0c0c0e] p-8 text-center text-zinc-100">
					<h1 className="text-lg font-semibold">Something went wrong</h1>
					<p className="max-w-md text-sm text-zinc-400">
						The editor hit an unexpected error and had to stop. Your project
						files are safe on disk.
					</p>
					<pre className="max-w-lg overflow-auto rounded bg-zinc-900 p-3 text-left text-[11px] text-red-400">
						{this.state.error.message}
					</pre>
					<button
						onClick={this.handleReload}
						className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-500 transition-colors"
					>
						Reload
					</button>
				</div>
			);
		}
		return this.props.children;
	}
}
