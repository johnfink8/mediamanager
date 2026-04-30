import React from "react";
import { BrowserRouter } from "react-router-dom";
import { SnackbarProvider } from "material-ui-snackbar-provider";
import MenuDrawer from "./MenuDrawer";
import {
    Environment,
    Network,
    RecordSource,
    Store,
    FetchFunction,
    Observable,
    SubscribeFunction,
} from "relay-runtime";
import { RelayEnvironmentProvider } from "react-relay";
import { createClient } from "graphql-ws";

const wsClient = createClient({
    url: "ws://localhost:8000/graphql",
});

const createEnvironment = () => {
    const fetchFunction: FetchFunction = (params, variables) => {
        const response = fetch("/graphql", {
            method: "POST",
            headers: [["Content-Type", "application/json"]],
            body: JSON.stringify({
                query: params.text,
                variables,
            }),
        });

        return Observable.from(response.then((data) => data.json()));
    };
    const subscribe: SubscribeFunction = (operation, variables) => {
        return Observable.create((sink) => {
            return wsClient.subscribe(
                {
                    operationName: operation.name,
                    query: operation.text || "",
                    variables,
                },
                // eslint-disable-next-line @typescript-eslint/ban-ts-comment
                // @ts-ignore
                sink
            );
        });
    };
    const network = Network.create(fetchFunction, subscribe);
    const store = new Store(new RecordSource());
    return new Environment({ store, network });
};

export default function App() {
    const environment = createEnvironment();
    return (
        <BrowserRouter>
            <RelayEnvironmentProvider environment={environment}>
                <SnackbarProvider SnackbarProps={{ autoHideDuration: 4000 }}>
                    <MenuDrawer />
                </SnackbarProvider>
            </RelayEnvironmentProvider>
        </BrowserRouter>
    );
}
