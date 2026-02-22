import React from "react";
import {
    Button,
    Card,
    CardMedia,
    CardContent,
    Typography,
    Grid,
    Chip,
    Stack,
} from "@mui/material";
import { graphql, useMutation } from "react-relay";
import AttributeChips from "./AttributeChips";
import { itemType } from "./types";

export const SetItemAddedMutation = graphql`
    mutation HistoricalItemDetailSetItemAddedMutation(
        $input: SetItemAddedInput!
    ) {
        setItemAdded(data: $input) {
            id
            added
        }
    }
`;

const HistoricalItemDetail: React.FC<{ item: itemType }> = ({ item }) => {
    const [commit] = useMutation(SetItemAddedMutation);

    const toggleAdded = (value: boolean) => {
        commit({
            variables: { input: { id: item.id, added: value } },
            optimisticResponse: {
                setItemAdded: { id: item.id, added: value },
            },
        });
    };

    return (
        <Grid item xs={12} sm={12} md={6}>
            <Card sx={{ position: "relative" }}>
                <CardContent>
                    <Stack
                        direction="row"
                        spacing={1}
                        alignItems="center"
                        sx={{ mb: 1 }}
                    >
                        <Typography variant="h6" sx={{ flex: 1 }}>
                            {item.checkedTitle ? item.checkedTitle : item.title}
                        </Typography>
                        <Chip
                            label={item.added ? "Marked Added" : "Dismissed"}
                            color={item.added ? "success" : "default"}
                            size="small"
                            sx={{ fontWeight: 600 }}
                        />
                    </Stack>
                    <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
                        <Button
                            variant={item.added ? "contained" : "outlined"}
                            onClick={() => toggleAdded(true)}
                            size="small"
                            disabled={item.added}
                        >
                            Mark as Added
                        </Button>
                        <Button
                            variant={!item.added ? "contained" : "outlined"}
                            onClick={() => toggleAdded(false)}
                            size="small"
                            disabled={!item.added}
                        >
                            Mark as Dismissed
                        </Button>
                    </Stack>
                    <AttributeChips item={item} />
                </CardContent>
                <CardMedia component="img" image={item.posterUrl || ""} />
            </Card>
        </Grid>
    );
};

export default React.memo(HistoricalItemDetail);
