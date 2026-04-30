import { graphql } from "react-relay";

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
