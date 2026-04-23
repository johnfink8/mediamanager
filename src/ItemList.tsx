import React, { FC, useEffect, useState } from "react";
import Grid from "@mui/material/Grid";
import Box from "@mui/material/Box";

import ItemDetail from "./ItemDetail";
import { MenuItemType } from "./types";
import BreadCrumbs from "./BreadCrumbs";
import {
    graphql,
    PreloadedQuery,
    usePreloadedQuery,
    useQueryLoader,
    useMutation,
} from "react-relay";
import { ItemListQuery } from "./__generated__/ItemListQuery.graphql";
import { ItemListAcceptAllRecommendedMutation } from "./__generated__/ItemListAcceptAllRecommendedMutation.graphql";
import {
    Alert,
    Backdrop,
    Button,
    Card,
    CardContent,
    Snackbar,
    Typography,
} from "@mui/material";

const ItemListQueryGQL = graphql`
    query ItemListQuery($itemType: String) {
        items(itemType: $itemType) {
            id
            nodes {
                id
                type
                uid
                title
                added
                checkedTitle
                posterUrl
                attributes {
                    key
                    values
                    details
                }
            }
        }
    }
`;

export const AddItemMutation = graphql`
    mutation ItemListAddItemMutation($input: AddItemInput!) {
        addItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const DeleteItemMutation = graphql`
    mutation ItemListDeleteItemMutation($input: AddItemInput!) {
        deleteItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const SetItemAddedMutation = graphql`
    mutation ItemListSetItemAddedMutation($input: SetItemAddedInput!) {
        setItemAdded(data: $input) {
            id
            added
        }
    }
`;

const AcceptAllRecommendedMutation = graphql`
    mutation ItemListAcceptAllRecommendedMutation(
        $input: AcceptAllRecommendedInput!
    ) {
        acceptAllRecommended(data: $input) {
            addedCount
            ignoredCount
            items {
                id
                nodes {
                    id
                    type
                    uid
                    title
                    added
                    checkedTitle
                    posterUrl
                    attributes {
                        key
                        values
                        details
                    }
                }
            }
        }
    }
`;

const RecheckVisibleMutation = graphql`
    mutation ItemListRecheckVisibleMutation($itemType: String!) {
        recheckVisible(itemType: $itemType) {
            id
            attributes {
                key
                values
                details
            }
        }
    }
`;

const ItemList: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<ItemListQuery>;
}> = ({ menuItem, queryRef }) => {
    const items = usePreloadedQuery(
        ItemListQueryGQL,
        queryRef
    ).items.nodes.filter(Boolean);
    const [recheckVisible, isRechecking] = useMutation(RecheckVisibleMutation);
    const [acceptAllRecommended, isAccepting] =
        useMutation<ItemListAcceptAllRecommendedMutation>(
            AcceptAllRecommendedMutation
        );
    const [acceptResult, setAcceptResult] = useState<{
        addedCount: number;
        ignoredCount: number;
    } | null>(null);
    const recheckItemType =
        menuItem.typeName === "mv" || menuItem.typeName === "tv"
            ? menuItem.typeName
            : null;

    return (
        <Box sx={{ position: "relative" }}>
            <BreadCrumbs crumbs={[menuItem]} />
            {recheckItemType && items.length > 0 && (
                <Box mb={2} sx={{ display: "flex", gap: 1 }}>
                    <Button
                        variant="outlined"
                        onClick={() =>
                            recheckVisible({
                                variables: { itemType: recheckItemType },
                            })
                        }
                        disabled={isRechecking}
                    >
                        Recheck
                    </Button>
                    <Button
                        variant="outlined"
                        onClick={() =>
                            acceptAllRecommended({
                                variables: {
                                    input: {
                                        ids: items.map((item) => item.id),
                                        itemType: recheckItemType,
                                    },
                                },
                                onCompleted(response) {
                                    const { addedCount, ignoredCount } =
                                        response.acceptAllRecommended;
                                    setAcceptResult({
                                        addedCount,
                                        ignoredCount,
                                    });
                                },
                            })
                        }
                        disabled={isAccepting}
                    >
                        Accept All Recommended
                    </Button>
                </Box>
            )}
            <Snackbar
                open={acceptResult !== null}
                autoHideDuration={6000}
                onClose={() => setAcceptResult(null)}
            >
                <Alert
                    onClose={() => setAcceptResult(null)}
                    severity="info"
                    sx={{ width: "100%" }}
                >
                    {acceptResult
                        ? `Added ${acceptResult.addedCount}, skipped ${acceptResult.ignoredCount}`
                        : ""}
                </Alert>
            </Snackbar>
            <Grid container spacing={2}>
                {items.map((item) => (
                    <ItemDetail key={item.uid} item={item} />
                ))}
            </Grid>
        </Box>
    );
};

const ItemListLoading: FC = () => (
    <Grid container spacing={2}>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <Backdrop
                    open
                    sx={{
                        position: "absolute",
                    }}
                />
                <CardContent>
                    <Typography variant="h6">Loading...</Typography>
                </CardContent>
            </Card>
        </Grid>
    </Grid>
);

const ItemListContainer: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const [queryRef, loadQuery, disposeQuery] =
        useQueryLoader<ItemListQuery>(ItemListQueryGQL);
    useEffect(() => {
        loadQuery({ itemType: menuItem.typeName });
        return () => {
            disposeQuery();
        };
    }, [menuItem, loadQuery, disposeQuery]);
    return queryRef ? (
        <ItemList menuItem={menuItem} queryRef={queryRef} />
    ) : (
        <ItemListLoading />
    );
};

export default React.memo(ItemListContainer);
