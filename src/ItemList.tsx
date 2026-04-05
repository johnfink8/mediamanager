import React, { FC, useEffect } from "react";
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
import { Backdrop, Button, Card, CardContent, Typography } from "@mui/material";

const ItemListQueryGQL = graphql`
    query ItemListQuery($filters: [Filter!]) {
        items(filters: $filters) {
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
    const items = usePreloadedQuery(ItemListQueryGQL, queryRef).items.nodes.filter(Boolean);
    const [recheckVisible, isRechecking] = useMutation(RecheckVisibleMutation);
    const canRecheck = (menuItem.typeName === "mv" || menuItem.typeName === "tv") && items.length > 0;

    return (
        <Box sx={{ position: "relative" }}>
            <BreadCrumbs crumbs={[menuItem]} />
            {canRecheck && (
                <Box mb={2}>
                    <Button
                        variant="outlined"
                        onClick={() => recheckVisible({ variables: { itemType: menuItem.typeName! } })}
                        disabled={isRechecking}
                    >
                        Recheck
                    </Button>
                </Box>
            )}
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
        loadQuery({ filters: [{ type: menuItem.typeName }] });
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
