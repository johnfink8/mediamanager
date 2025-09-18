import {
    Button,
    Card,
    CardMedia,
    CardContent,
    Link,
    Typography,
    Grid,
} from "@mui/material";
import React from "react";
import { itemType } from "./types";
import { itemLink } from "./util";
import { useMutation } from "react-relay";
import { AddItemMutation, DeleteItemMutation } from "./ItemList";
import { ItemListAddItemMutation } from "./__generated__/ItemListAddItemMutation.graphql";
import { ItemListDeleteItemMutation } from "./__generated__/ItemListDeleteItemMutation.graphql";
import AttributeChips from "./AttributeChips";


const ItemDetail: React.FC<{
    item: itemType;
}> = ({ item }) => {
    const [addItem] = useMutation<ItemListAddItemMutation>(AddItemMutation);
    const [deleteItem] =
        useMutation<ItemListDeleteItemMutation>(DeleteItemMutation);
    const handleAdd = (item: itemType) => {
        addItem({
            variables: {
                input: {
                    id: item.id,
                },
            },
        });
    };
    const handleDelete = (item: itemType) => {
        deleteItem({
            variables: {
                input: {
                    id: item.id,
                },
            },
        });
    };

    return (
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <CardContent>
                    <Typography variant="h6">
                        {item.checkedTitle ? item.checkedTitle : item.title}
                    </Typography>
                    <Button onClick={() => handleAdd(item)}>Add</Button>
                    <Button onClick={() => handleDelete(item)}>Ignore</Button>
                    <Link href={itemLink(item)} color="primary" target="_blank">
                        Details
                    </Link>
                    {/* Attribute chips */}
                    <AttributeChips item={item} />
                </CardContent>
                <CardMedia component="img" image={item.posterUrl || ""} />
            </Card>
        </Grid>
    );
};
export default React.memo(ItemDetail);
